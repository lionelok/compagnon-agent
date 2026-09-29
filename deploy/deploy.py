"""Provision and update the Lifestyle Companion on AWS.

    python deploy/deploy.py create   # bucket, IAM role, security group, EC2, CloudFront (HTTPS)
    python deploy/deploy.py update   # re-package the app and restart it on the instance (via SSM)
    python deploy/deploy.py status
    python deploy/deploy.py fetch-contacts   # copy the live contact history file to data/contact_history.jsonl

Uses the AWS credentials in the environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN).
All resources are named lifestyle-companion-v2* and recorded in deploy/outputs.json (no secrets).
"""
import io
import json
import sys
import tarfile
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).with_name('outputs.json')
NAME = 'lifestyle-companion-v2'
REGION = 'us-east-1'
INSTANCE_TYPE = 't3.medium'
FILES = ['store.py', 'basket_tools.py', 'order_tools.py', 'tool_adapter.py', 'tool_schemas.json',
         'requirements-app.txt', 'companion', 'data/full/customers.csv', 'data/full/products.csv', 'data/full/insights.db']
CACHING_DISABLED = '4135ea2d-6df8-44a3-9df3-4b5a84be39ad'
ALL_VIEWER_EXCEPT_HOST = 'b689b0a8-53d0-40ab-baf2-68738e2966ac'

session = boto3.Session(region_name=REGION)
s3, iam, ec2, ssm, cf = (session.client(x) for x in ('s3', 'iam', 'ec2', 'ssm', 'cloudfront'))
ACCOUNT = session.client('sts').get_caller_identity()['Account']
BUCKET = f'{NAME}-{ACCOUNT}'


def outputs():
    return json.loads(OUT.read_text()) if OUT.exists() else {}


def save(**kw):
    data = outputs() | kw
    OUT.write_text(json.dumps(data, indent=2))
    return data


def package():
    buf = io.BytesIO()
    skip = lambda ti: None if '__pycache__' in ti.name or ti.name.endswith('.DS_Store') else ti
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for f in FILES:
            tar.add(ROOT / f, arcname=f, filter=skip)
    buf.seek(0)
    s3.upload_fileobj(buf, BUCKET, 'app.tar.gz')
    print(f'uploaded s3://{BUCKET}/app.tar.gz ({buf.getbuffer().nbytes / 1e6:.1f} MB)')


INSTALL = f"""#!/bin/bash
set -euxo pipefail
dnf install -y python3.11 python3.11-pip
id companion || useradd -r -m -d /opt/companion companion
mkdir -p /opt/companion/app /opt/companion/state
aws s3 cp s3://{BUCKET}/app.tar.gz /tmp/app.tar.gz --region {REGION}
rm -rf /opt/companion/app.new && mkdir /opt/companion/app.new && tar xzf /tmp/app.tar.gz -C /opt/companion/app.new
[ -d /opt/companion/venv ] || python3.11 -m venv /opt/companion/venv
/opt/companion/venv/bin/pip install -q -r /opt/companion/app.new/requirements-app.txt
rm -rf /opt/companion/app && mv /opt/companion/app.new /opt/companion/app
chown -R companion:companion /opt/companion
cat > /etc/systemd/system/companion.service <<'EOF'
[Unit]
Description=Lifestyle Companion
After=network-online.target
[Service]
User=companion
WorkingDirectory=/opt/companion/app
Environment=AWS_REGION={REGION} AWS_DEFAULT_REGION={REGION} STATE_DIR=/opt/companion/state PYTHONUNBUFFERED=1
ExecStart=/opt/companion/venv/bin/uvicorn companion.server:app --host 0.0.0.0 --port 80 --timeout-keep-alive 75
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable companion
systemctl restart companion
"""


def create():
    # 1. Bucket for the app bundle (private).
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    s3.put_public_access_block(Bucket=BUCKET, PublicAccessBlockConfiguration={
        'BlockPublicAcls': True, 'IgnorePublicAcls': True, 'BlockPublicPolicy': True, 'RestrictPublicBuckets': True})
    package()

    # 2. Instance role: Bedrock (Claude), Polly (voice), the bundle, SSM for updates.
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'ec2.amazonaws.com'},
                                                     'Action': 'sts:AssumeRole'}]}
    try:
        iam.create_role(RoleName=NAME, AssumeRolePolicyDocument=json.dumps(trust), Description='Lifestyle Companion v2')
    except iam.exceptions.EntityAlreadyExistsException:
        pass
    iam.put_role_policy(RoleName=NAME, PolicyName='runtime', PolicyDocument=json.dumps({
        'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'], 'Resource': '*'},
            {'Effect': 'Allow', 'Action': ['polly:SynthesizeSpeech'], 'Resource': '*'},
            {'Effect': 'Allow', 'Action': ['s3:GetObject'], 'Resource': f'arn:aws:s3:::{BUCKET}/*'},
            {'Effect': 'Allow', 'Action': ['s3:PutObject'], 'Resource': f'arn:aws:s3:::{BUCKET}/contacts/*'}]}))
    iam.attach_role_policy(RoleName=NAME, PolicyArn='arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore')
    try:
        iam.create_instance_profile(InstanceProfileName=NAME)
        iam.add_role_to_instance_profile(InstanceProfileName=NAME, RoleName=NAME)
        time.sleep(12)  # instance profile propagation
    except iam.exceptions.EntityAlreadyExistsException:
        pass

    # 3. Security group: HTTP only from CloudFront's origin-facing addresses.
    vpc = ec2.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])['Vpcs'][0]['VpcId']
    existing = ec2.describe_security_groups(Filters=[{'Name': 'group-name', 'Values': [NAME]}, {'Name': 'vpc-id', 'Values': [vpc]}])['SecurityGroups']
    if existing:
        sg = existing[0]['GroupId']
    else:
        sg = ec2.create_security_group(GroupName=NAME, Description='Lifestyle Companion v2 - CloudFront only', VpcId=vpc)['GroupId']
        pl = ec2.describe_managed_prefix_lists(Filters=[{'Name': 'prefix-list-name', 'Values': ['com.amazonaws.global.cloudfront.origin-facing']}])['PrefixLists'][0]['PrefixListId']
        ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{'IpProtocol': 'tcp', 'FromPort': 80, 'ToPort': 80, 'PrefixListIds': [{'PrefixListId': pl}]}])

    # 4. EC2 instance (Amazon Linux 2023).
    out = outputs()
    if not out.get('instance_id'):
        ami = ssm.get_parameter(Name='/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64')['Parameter']['Value']
        inst = ec2.run_instances(
            ImageId=ami, InstanceType=INSTANCE_TYPE, MinCount=1, MaxCount=1, UserData=INSTALL,
            IamInstanceProfile={'Name': NAME}, SecurityGroupIds=[sg], MetadataOptions={'HttpTokens': 'required'},
            BlockDeviceMappings=[{'DeviceName': '/dev/xvda', 'Ebs': {'VolumeSize': 20, 'VolumeType': 'gp3'}}],
            TagSpecifications=[{'ResourceType': 'instance', 'Tags': [{'Key': 'Name', 'Value': NAME}]}])['Instances'][0]
        out = save(instance_id=inst['InstanceId'], security_group=sg, bucket=BUCKET, role=NAME)
    ec2.get_waiter('instance_running').wait(InstanceIds=[out['instance_id']])
    dns = ec2.describe_instances(InstanceIds=[out['instance_id']])['Reservations'][0]['Instances'][0]['PublicDnsName']
    out = save(origin=dns)

    # 5. CloudFront in front of the instance: public HTTPS URL, no caching, streaming-friendly.
    if not out.get('distribution_id'):
        d = cf.create_distribution(DistributionConfig={
            'CallerReference': f'{NAME}-{int(time.time())}', 'Comment': NAME, 'Enabled': True, 'HttpVersion': 'http2',
            'PriceClass': 'PriceClass_100',
            'Origins': {'Quantity': 1, 'Items': [{
                'Id': 'ec2', 'DomainName': dns,
                'CustomOriginConfig': {'HTTPPort': 80, 'HTTPSPort': 443, 'OriginProtocolPolicy': 'http-only',
                                       'OriginReadTimeout': 60, 'OriginKeepaliveTimeout': 30}}]},
            'DefaultCacheBehavior': {
                'TargetOriginId': 'ec2', 'ViewerProtocolPolicy': 'redirect-to-https', 'Compress': False,
                'CachePolicyId': CACHING_DISABLED, 'OriginRequestPolicyId': ALL_VIEWER_EXCEPT_HOST,
                'AllowedMethods': {'Quantity': 7, 'Items': ['GET', 'HEAD', 'OPTIONS', 'PUT', 'POST', 'PATCH', 'DELETE'],
                                   'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}}}})['Distribution']
        out = save(distribution_id=d['Id'], url=f"https://{d['DomainName']}")
    print(json.dumps(out, indent=2))


def run_on_instance(script, instance_id):
    cmd = ssm.send_command(InstanceIds=[instance_id], DocumentName='AWS-RunShellScript',
                           Parameters={'commands': [script]}, TimeoutSeconds=600)['Command']['CommandId']
    for _ in range(90):
        time.sleep(5)
        try:
            r = ssm.get_command_invocation(CommandId=cmd, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if r['Status'] not in ('Pending', 'InProgress', 'Delayed'):
            return r
    raise TimeoutError('SSM command did not finish')


def fetch_contacts():
    out = outputs()
    iam.put_role_policy(RoleName=NAME, PolicyName='contacts-export', PolicyDocument=json.dumps({
        'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': ['s3:PutObject'],
                                                'Resource': f'arn:aws:s3:::{BUCKET}/contacts/*'}]}))
    time.sleep(15)  # IAM changes take a few seconds to reach the instance role
    r = run_on_instance(f'for i in 1 2 3 4 5; do aws s3 cp /opt/companion/state/contact_history.jsonl s3://{BUCKET}/contacts/contact_history.jsonl '
                        f'--region {REGION} && break; sleep 10; done', out['instance_id'])
    if r['Status'] != 'Success':
        sys.exit(f"export failed: {r['StandardErrorContent'][-800:]}")
    target = ROOT / 'data' / 'contact_history.jsonl'
    s3.download_file(BUCKET, 'contacts/contact_history.jsonl', str(target))
    print(f'{target} ({sum(1 for _ in target.open())} contacts)')


def update():
    out = outputs()
    package()
    cmd = ssm.send_command(InstanceIds=[out['instance_id']], DocumentName='AWS-RunShellScript',
                           Parameters={'commands': [INSTALL]}, TimeoutSeconds=600)['Command']['CommandId']
    for _ in range(90):
        time.sleep(5)
        try:
            r = ssm.get_command_invocation(CommandId=cmd, InstanceId=out['instance_id'])
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        if r['Status'] not in ('Pending', 'InProgress', 'Delayed'):
            print(r['Status'], r['StandardErrorContent'][-1500:] if r['Status'] != 'Success' else '')
            return
    print('timed out waiting for SSM')


def status():
    out = outputs()
    print(json.dumps(out, indent=2))
    if out.get('distribution_id'):
        print('CloudFront:', cf.get_distribution(Id=out['distribution_id'])['Distribution']['Status'])


if __name__ == '__main__':
    {'create': create, 'update': update, 'status': status, 'fetch-contacts': fetch_contacts}[sys.argv[1] if len(sys.argv) > 1 else 'status']()
