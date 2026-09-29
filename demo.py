"""Safe repeatable automated demonstration, no LLM or cloud credentials required."""
import json
import tempfile
from pathlib import Path
from store import Store
from tool_adapter import call_tool

def show(label, value):
    print('\n' + label)
    print(json.dumps(value, indent=2))

with tempfile.TemporaryDirectory() as directory:
    store = Store(db_path=Path(directory)/'demo.sqlite')
    user = 'U000001'
    show('1. Customer requests Audio 02', call_tool(store,user,'add_to_basket',{'product_id':'I0001'}))
    quote = call_tool(store,user,'prepare_checkout',{})
    show('2. Summary requiring confirmation',quote)
    # This scripted demo explicitly simulates the customer confirming the summary.
    # A real UI must wait for a confirmation click or a clear confirmation turn.
    order = store.confirm_order(user,quote['data']['checkout_id'],customer_confirmed=True)
    show('3. Simulated customer confirmation creates an order',order)
    show('4. Basket after checkout',call_tool(store,user,'get_basket',{}))
    repeated = store.confirm_order(user,quote['data']['checkout_id'],customer_confirmed=True)
    print('\nRepeated confirmation returns the same order:', repeated['order_id']==order['order_id'])
