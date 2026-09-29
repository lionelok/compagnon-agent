# Gen AI Use Case Hackathon Challenge: AI Lifestyle Companion

Build an AI companion that supports **voice and text** and helps customers
**plan for their needs, discover relevant products, choose suitable options and
complete a simulated purchase**.

Use customer profiles, past interactions, the product catalogue and recommender
model output to personalize the experience across Home, Shop and Rewards.

The companion should:

1. **Understand customer needs:** Identify their goal, budget and preferences.
   Ask follow-up questions when needed.
2. **Recommend suitable choices:** Find relevant products, explain recommendations
   and help customers compare options.
3. **Engage proactively and cross-sell:** Use available data to start helpful
   conversations and suggest complementary products when relevant. Teams decide
   when and how.
4. **Adapt to feedback:** Update suggestions when customers change their needs
   or reject an option.
5. **Support a simulated purchase:** Use the supplied functions to manage the
   basket, show the checkout summary and create an order after customer confirmation.

## Data and resources

| Resource | Purpose |
|---|---|
| `train` | Customer profiles for personalization. Exclude `relevant_items` from the companion's inputs: it contains ML target labels. |
| `products` | Product catalogue for discovery, comparison, eligibility checks and prices. |
| `interactions` | Customer activity across Home, Shop and Rewards. Join to profiles using `user_id` and to products using `product_id`. |
| Recommender model output | Ranked choices for each customer. Combine these with the customer's current request. The distribution filename is to be confirmed. |
| `basket_tools.py` | Functions to view, add, update and remove basket items. |
| `order_tools.py` | Functions to prepare checkout, confirm a simulated order and retrieve it. |
| `store.py` | Data loading, eligibility checks, price calculations and SQLite storage. |
| `tool_adapter.py` | Optional helper to dispatch agent tool calls and return structured results. |
| `tool_schemas.json` | Descriptions and inputs for the six model-visible tools. |
| `data/customers.csv`, `data/products.csv` | Small samples for testing this starter; replace/configure these with the full challenge datasets. |
| `demo.py`, `test_starter.py` | Runnable simulation example and automated tests. |
| `requirements.txt` | Python dependency information for the starter. |
| `AI_Companion_Functions.xlsx` | Separately supplied function reference sheet. |
| Provided AWS account | Build and host the solution using the services available in the account. |

The supplied dataset names above do not specify file extensions. Use the actual
paths and filenames distributed for the challenge. Do not expose hidden test
answers to the companion.

## What this package provides

This package implements the basket and order simulation. Teams build the voice/text
interface, customer understanding, planning, product discovery, recommender-output
integration, proactive engagement and cross-selling around these functions.
Teams choose the agent framework and conversation design.

## Run the starter

Python 3.10+ is required. The simulation uses only the Python standard library.
Add dependencies for your chosen AI, voice and interface components separately.

Extract the complete ZIP, keep its folders intact, open a terminal in
`ai_companion_starter`, and run:

```bash
python demo.py
python -m unittest -v
```

Use `python3` if needed. The demo is a scripted function test, not a chatbot.
It adds a sample product, prepares checkout, simulates customer confirmation,
creates an order and checks that repeated confirmation does not duplicate it.
Its temporary database is deleted when the demo finishes.

If using Colab, ensure the two sample CSVs are in the `data` folder beside
`store.py`. Uploading only the Python files is not sufficient.

## Connect your companion

### 1. Load the data

```python
from store import Store

store = Store()  # Loads sample CSVs and creates simulation.sqlite
```

For the full datasets, pass their actual file paths using `products_path` and
`customers_path`. The `customers_path` argument should point to the customer
profile dataset named `train`. CSVs use a semicolon separator.

```python
store = Store(
    db_path='team.sqlite',
    products_path=products_file_path,
    customers_path=train_file_path
)
```

Set `products_file_path` and `train_file_path` to the distributed file paths first.
Restart the application after changing its input CSVs. Interactions and recommender
output are used by the team's personalization logic, separately from this loader.

### 2. Select a demo customer

Provide a customer selector in the interface. Store the selected `user_id` in the
application session and use it consistently for profiles, history, recommendations,
basket and orders. No real customer login is required for this demo. Switching
customers starts a separate conversation and loads that customer's own basket.
Voice and text should share the same customer and conversation context.

### 3. Connect the functions

Register the supplied functions with your chosen agent framework. The application
supplies `store` and the selected customer ID. The agent supplies tool-specific
inputs such as the product ID and quantity.

You can use `call_tool` or your framework's own tool-execution mechanism.

```python
from tool_adapter import call_tool

result = call_tool(
    store,
    session_user_id='U000001',
    name='add_to_basket',
    arguments={'product_id': 'I0001', 'quantity': 1}
)
```

With `call_tool`, success returns `{"ok": true, "data": {...}}` and a business-rule
error returns `{"ok": false, "error": {"code": "...", "message": "..."}}`.
Direct function calls return the payload or raise `ToolError`.
Only tell the customer that an action succeeded after the function succeeds.

### 4. Obtain customer confirmation

Call `prepare_checkout` and present its summary. Retain the returned `checkout_id`.
After the customer explicitly confirms that summary, the application calls:

```python
from order_tools import confirm_order

order = confirm_order(
    store,
    user_id,
    checkout_id=checkout['checkout_id'],
    customer_confirmed=True
)
```

The application supplies `customer_confirmed=True` only after customer confirmation
through the interface or conversation. It must not treat a model-generated boolean
as customer approval. `confirm_order` is therefore not included in the supplied
model-visible tool schemas or optional dispatcher.

If the basket or prices change, prepare a new checkout summary and obtain fresh
confirmation. Repeating a successful confirmation returns the same order.

## Supplied functions

| Function | Tool-specific input | Output |
|---|---|---|
| `get_basket` | None | Current basket and total. |
| `add_to_basket` | `product_id`, `quantity` (default 1) | Updated basket. |
| `update_basket` | `product_id`, `quantity` | Updated basket; zero removes the item. |
| `remove_from_basket` | `product_id` | Updated basket. |
| `prepare_checkout` | None | Checkout ID and summary requiring confirmation. |
| `confirm_order` | `checkout_id`, `customer_confirmed` | Saved simulated order; application call after confirmation. |
| `get_order` | `order_id` | Saved order for the current customer. |

All functions also receive the configured `store` and current `user_id` from the
application. Use the separately supplied function sheet for further details.

