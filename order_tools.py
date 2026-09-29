def prepare_checkout(store, user_id):
    return store.prepare_checkout(user_id)

def confirm_order(store, user_id, checkout_id, customer_confirmed=False):
    return store.confirm_order(user_id, checkout_id, customer_confirmed)

def get_order(store, user_id, order_id):
    return store.get_order(user_id, order_id)
