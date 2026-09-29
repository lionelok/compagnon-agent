"""Bind user_id from the application session, never from model-generated arguments."""
from store import ToolError
import basket_tools
import order_tools

FUNCTIONS = {name: getattr(basket_tools, name) for name in
             ('get_basket','add_to_basket','update_basket','remove_from_basket')}
FUNCTIONS.update({name: getattr(order_tools, name) for name in ('prepare_checkout','get_order')})
ALLOWED = {'get_basket':set(), 'add_to_basket':{'product_id','quantity'},
           'update_basket':{'product_id','quantity'}, 'remove_from_basket':{'product_id'},
           'prepare_checkout':set(), 'get_order':{'order_id'}}

def call_tool(store, session_user_id, name, arguments):
    try:
        if name not in FUNCTIONS:
            raise ToolError('UNKNOWN_TOOL', 'Tool is not available to the agent.')
        if not isinstance(arguments, dict) or set(arguments)-ALLOWED[name]:
            raise ToolError('INVALID_ARGUMENTS', 'Unexpected tool arguments.')
        try:
            data = FUNCTIONS[name](store, session_user_id, **arguments)
        except TypeError:
            raise ToolError('INVALID_ARGUMENTS', 'Required tool arguments are missing or invalid.')
        return {'ok':True, 'data':data}
    except ToolError as e:
        return {'ok':False, 'error':{'code':e.code, 'message':e.message}}
