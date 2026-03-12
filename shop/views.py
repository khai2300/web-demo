from .views_account import account, add_address, delete_address, set_default_address
from .views_admin import (
    admin_dashboard,
    admin_orders,
    admin_products,
    admin_promotions,
    admin_users,
)
from .views_auth import login_view, logout_view, register_view
from .views_cart import add_to_cart, cart, remove_cart, update_cart
from .views_chat import chat_api, chat_reset, chat_view
from .views_orders import (
    cancel_order,
    checkout,
    checkout_success,
    order_trace_qr,
    orders,
    trace_order,
)
from .views_public import (
    home,
    news_list,
    product_detail,
    product_trace_qr,
    search_suggest,
    trace_product,
)

__all__ = [
    "home",
    "news_list",
    "search_suggest",
    "register_view",
    "login_view",
    "logout_view",
    "product_detail",
    "product_trace_qr",
    "trace_product",
    "cart",
    "add_to_cart",
    "update_cart",
    "remove_cart",
    "checkout",
    "account",
    "add_address",
    "delete_address",
    "set_default_address",
    "orders",
    "cancel_order",
    "order_trace_qr",
    "trace_order",
    "checkout_success",
    "chat_view",
    "chat_api",
    "chat_reset",
    "admin_dashboard",
    "admin_products",
    "admin_orders",
    "admin_users",
    "admin_promotions",
]
