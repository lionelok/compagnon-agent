"""Thin, framework-neutral functions; pass a Store instance and trusted customer ID."""
def get_basket(store, user_id):
    return store.get_basket(user_id)

def add_to_basket(store, user_id, product_id, quantity=1):
    return store.change(user_id, product_id, quantity, 'add')

def update_basket(store, user_id, product_id, quantity):
    return store.change(user_id, product_id, quantity, 'set')

def remove_from_basket(store, user_id, product_id):
    return store.change(user_id, product_id, 0, 'set')
