import difflib
import html
import json
import os
import re
import socket
from datetime import timedelta
from io import BytesIO
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
import qrcode

from .models import (
    Address,
    CartItem,
    Category,
    ChatMessage,
    ChatSession,
    Order,
    OrderTraceToken,
    OrderItem,
    Product,
    ProductionZone,
    Promotion,
    UserProfile,
)
from .services.chat_ai import generate_chat_reply, quick_replies

User = get_user_model()
PAYMENT_METHOD_COD = "COD"
PAYMENT_METHOD_BANK_TRANSFER = "Bank Transfer"
PAYMENT_METHODS = [
    {"value": PAYMENT_METHOD_COD, "label": "Thanh toan khi nhan hang (COD)"},
    {"value": PAYMENT_METHOD_BANK_TRANSFER, "label": "Thanh toan online (Ngan hang)"},
]
PAYMENT_METHOD_VALUES = {method["value"] for method in PAYMENT_METHODS}
IFRAME_SRC_RE = re.compile(r"""src=(["'])(?P<src>.+?)\1""", re.IGNORECASE)
REVENUE_STATUSES = [Order.STATUS_PROCESSING, Order.STATUS_SHIPPED, Order.STATUS_DELIVERED]
DASHBOARD_PERIODS = {
    "7d": {"label": "7 ngay gan nhat", "days": 7, "bucket": "day"},
    "30d": {"label": "30 ngay gan nhat", "days": 30, "bucket": "day"},
    "90d": {"label": "90 ngay gan nhat", "days": 90, "bucket": "week"},
    "12m": {"label": "12 thang gan nhat", "days": 365, "bucket": "month"},
}


def _normalize_map_link(raw_value):
    value = (raw_value or "").strip()
    if not value:
        return ""

    if "<iframe" in value.lower():
        match = IFRAME_SRC_RE.search(value)
        if not match:
            return ""
        value = html.unescape(match.group("src")).strip()

    if not value.lower().startswith(("http://", "https://")):
        return ""

    return value[:2000]


def ensure_seed_data():
    zones = _ensure_production_zones()
    if Category.objects.exists() and Product.objects.exists():
        _sync_sample_product_zones(zones)
        _assign_default_source_zones(default_zone=zones.get("TN-TN-01"))
        return

    category_map = {}
    for name in ["Che xanh", "Che o long", "Che thao moc", "Che dac san"]:
        category, _ = Category.objects.get_or_create(name=name)
        category_map[name] = category

   

    for row in products:
        Product.objects.get_or_create(
            name=row["name"],
            defaults={
                "category": category_map[row["category"]],
                "price": row["price"],
                "stock": row["stock"],
                "source_zone": (
                    zones.get("HG-ST-02")
                    if row["name"] == "Bach Tra Shan Tuyet"
                    else zones.get("BL-OL-03")
                    if row["name"] == "O Long Bup Xoan"
                    else zones.get("TN-TN-01")
                ),
                "short_description": row["short_description"],
                "description": row["description"],
                "image_url": row["image_url"],
            },
        )

    Promotion.objects.get_or_create(
        code="TET2026",
        defaults={
            "discount_type": Promotion.DISCOUNT_PERCENT,
            "value": Decimal("10"),
            "is_active": True,
        },
    )

    if not User.objects.filter(username="admin").exists():
        admin = User.objects.create_user(
            username="admin",
            email="admin@tea.local",
            password="admin123",
            is_staff=True,
            is_superuser=True,
        )
        UserProfile.objects.get_or_create(user=admin, defaults={"full_name": "Admin", "phone": "0900000000"})
    _sync_sample_product_zones(zones)


def _ensure_production_zones():
    zones = {}
    zone_seed = [
        {
            "code": "TN-TN-01",
            "name": "Vung che Tan Cuong",
            "province": "Thai Nguyen",
            "latitude": Decimal("21.594700"),
            "longitude": Decimal("105.773300"),
            "description": "Vung trong che bup truyen thong, do cao trung binh, dat feralit.",
        },
        {
            "code": "HG-ST-02",
            "name": "Vung che Shan Tuyet Tay Con Linh",
            "province": "Ha Giang",
            "latitude": Decimal("22.788300"),
            "longitude": Decimal("104.978900"),
            "description": "Cay che co thu vung cao, thu hoach thu cong.",
        },
        {
            "code": "BL-OL-03",
            "name": "Vung O Long Bao Loc",
            "province": "Lam Dong",
            "latitude": Decimal("11.547700"),
            "longitude": Decimal("107.807800"),
            "description": "Vung o long chuyen canh theo mo hinh huu co.",
        },
    ]
    for row in zone_seed:
        zone, _ = ProductionZone.objects.get_or_create(code=row["code"], defaults=row)
        zones[zone.code] = zone
    return zones


def _assign_default_source_zones(default_zone=None):
    if default_zone is None:
        default_zone = ProductionZone.objects.order_by("id").first()
    if not default_zone:
        return
    Product.objects.filter(source_zone__isnull=True).update(source_zone=default_zone)


def _sync_sample_product_zones(zones):
    mapping = {
        "Che Bup Thai Nguyen": "TN-TN-01",
        "Tra Hoa Cuc Mat Ong": "TN-TN-01",
        "Bach Tra Shan Tuyet": "HG-ST-02",
        "O Long Bup Xoan": "BL-OL-03",
    }
    for product_name, zone_code in mapping.items():
        zone = zones.get(zone_code)
        if not zone:
            continue
        Product.objects.filter(name=product_name).update(source_zone=zone)


def _get_or_create_order_trace_token(order):
    token_obj, _ = OrderTraceToken.objects.get_or_create(order=order)
    return token_obj


def _build_public_url(request, path):
    base_url = (getattr(settings, "QR_PUBLIC_BASE_URL", "") or "").strip()
    if base_url:
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    scheme = "https" if request.is_secure() else "http"
    host = request.get_host().strip()
    if host and not host.startswith(("127.0.0.1", "localhost", "[::1]", "::1")):
        return f"{scheme}://{host}/{path.lstrip('/')}"

    # Fallback when admin is opened via localhost: try LAN IP so phone can access QR URL.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            lan_ip = sock.getsockname()[0]
    except OSError:
        lan_ip = ""

    if lan_ip and not lan_ip.startswith("127."):
        port = request.get_port()
        default_port = "443" if scheme == "https" else "80"
        host_with_port = f"{lan_ip}:{port}" if port and port != default_port else lan_ip
        return f"{scheme}://{host_with_port}/{path.lstrip('/')}"

    return request.build_absolute_uri(path)


def _build_order_trace_url(request, order):
    token_obj = _get_or_create_order_trace_token(order)
    trace_path = reverse("shop:trace_order", kwargs={"token": token_obj.token})
    return _build_public_url(request, trace_path)


def _calculate_cart_summary(user, promo_code=""):
    cart_items = list(CartItem.objects.select_related("product").filter(user=user))
    subtotal = sum((item.product.price * item.quantity for item in cart_items), Decimal("0"))
    discount = Decimal("0")
    promotion = None

    normalized_code = (promo_code or "").strip().upper()
    if normalized_code:
        now = timezone.now()
        promotion = Promotion.objects.filter(code=normalized_code, is_active=True).first()
        if promotion:
            valid_start = promotion.start_at is None or promotion.start_at <= now
            valid_end = promotion.end_at is None or promotion.end_at >= now
            if not (valid_start and valid_end):
                promotion = None
        if promotion:
            if promotion.discount_type == Promotion.DISCOUNT_PERCENT:
                discount = subtotal * promotion.value / Decimal("100")
            else:
                discount = promotion.value

    discount = min(discount, subtotal)
    total = subtotal - discount
    return {
        "cart_items": cart_items,
        "subtotal": subtotal,
        "discount": discount,
        "total": total,
        "promotion": promotion,
        "promo_code": normalized_code,
    }


def _build_bank_transfer_info(total_amount, username=""):
    bank_name = (getattr(settings, "BANK_TRANSFER_BANK_NAME", "") or "Techcombank").strip()
    bank_code = (getattr(settings, "BANK_TRANSFER_BANK_CODE", "") or "TCB").strip().upper()
    account_name = (getattr(settings, "BANK_TRANSFER_ACCOUNT_NAME", "") or "NGUYEN TRI KHAI").strip()
    account_number = (getattr(settings, "BANK_TRANSFER_ACCOUNT_NUMBER", "") or "19037577368017").strip()
    note_prefix = (getattr(settings, "BANK_TRANSFER_NOTE_PREFIX", "") or "THANH TOAN").strip()
    amount_int = int(total_amount) if total_amount else 0
    transfer_note = f"{note_prefix} {username}".strip()

    qr_base = f"https://img.vietqr.io/image/{bank_code}-{account_number}-compact2.png"
    query = urlencode(
        {
            "amount": amount_int,
            "addInfo": transfer_note,
            "accountName": account_name,
        }
    )
    return {
        "method_value": PAYMENT_METHOD_BANK_TRANSFER,
        "bank_name": bank_name,
        "bank_code": bank_code,
        "account_name": account_name,
        "account_number": account_number,
        "transfer_note": transfer_note,
        "qr_url": f"{qr_base}?{query}",
    }



@require_GET
def home(request):
    ensure_seed_data()
    q = request.GET.get("q", "").strip()
    selected_category = request.GET.get("category", "").strip()

    products = Product.objects.select_related("category").all()
    categories = Category.objects.all()

    if selected_category.isdigit():
        products = products.filter(category_id=int(selected_category))
    if q:
        products = products.filter(
            Q(name__icontains=q) | Q(description__icontains=q) | Q(category__name__icontains=q)
        )

    return render(
        request,
        "shop/home.html",
        {
            "products": products,
            "categories": categories,
            "q": q,
            "selected_category": selected_category,
        },
    )


@require_GET
def search_suggest(request):
    q = request.GET.get("q", "").strip()
    if not q:
        return JsonResponse([], safe=False)

    names = list(Product.objects.values_list("name", flat=True))
    contains = [name for name in names if q.lower() in name.lower()]
    fuzzy = difflib.get_close_matches(q, names, n=8, cutoff=0.3)
    merged = []
    for item in contains + fuzzy:
        if item not in merged:
            merged.append(item)
    return JsonResponse(merged[:8], safe=False)


def register_view(request):
    if request.user.is_authenticated:
        return redirect("shop:home")

    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        phone = request.POST.get("phone", "").strip()
        email = request.POST.get("email", "").strip().lower()
        username = request.POST.get("username", "").strip()
        password1 = request.POST.get("password1", "")
        password2 = request.POST.get("password2", "")

        if not all([full_name, phone, email, username, password1, password2]):
            messages.error(request, "Vui long nhap day du thong tin.")
            return redirect("shop:register")
        if password1 != password2:
            messages.error(request, "Mat khau xac nhan khong khop.")
            return redirect("shop:register")
        if User.objects.filter(username=username).exists():
            messages.error(request, "Username da ton tai.")
            return redirect("shop:register")
        if User.objects.filter(email=email).exists():
            messages.error(request, "Email da ton tai.")
            return redirect("shop:register")

        user = User.objects.create_user(username=username, email=email, password=password1)
        UserProfile.objects.create(user=user, full_name=full_name, phone=phone)
        messages.success(request, "Dang ky thanh cong. Ban co the dang nhap ngay.")
        return redirect("shop:login")

    return render(request, "shop/auth/register.html")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("shop:home")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        auth_username = username
        if "@" in username:
            user_obj = User.objects.filter(email__iexact=username).first()
            auth_username = user_obj.username if user_obj else username

        user = authenticate(request, username=auth_username, password=password)
        if user is None:
            messages.error(request, "Thong tin dang nhap khong dung.")
            return redirect("shop:login")
        if not user.is_active:
            messages.error(request, "Tai khoan da bi khoa.")
            return redirect("shop:login")
        login(request, user)
        messages.success(request, "Dang nhap thanh cong.")
        return redirect("shop:home")

    return render(request, "shop/auth/login.html")


@login_required
def logout_view(request):
    logout(request)
    messages.info(request, "Ban da dang xuat.")
    return redirect("shop:login")


@require_GET
def product_detail(request, product_id):
    product = get_object_or_404(Product.objects.select_related("category", "source_zone"), id=product_id)
    related_products = Product.objects.filter(category=product.category).exclude(id=product.id)[:3]
    return render(
        request,
        "shop/product_detail.html",
        {
            "product": product,
            "related_products": related_products,
        },
    )


@require_GET
def product_trace_qr(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    trace_path = reverse("shop:trace_product", kwargs={"product_id": product.id})
    trace_url = _build_public_url(request, trace_path)
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(trace_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#1d3b2c", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    response = HttpResponse(buffer.getvalue(), content_type="image/png")
    if request.GET.get("download", "").strip().lower() in {"1", "true", "yes"}:
        response["Content-Disposition"] = f'attachment; filename="trace-product-{product.id}.png"'
    return response


@require_GET
def trace_product(request, product_id):
    product = get_object_or_404(Product.objects.select_related("source_zone", "category"), id=product_id)
    zone = product.source_zone
    if zone:
        zone_data = {
            "name": zone.name,
            "code": zone.code,
            "province": zone.province,
            "description": zone.description,
            "latitude": float(zone.latitude),
            "longitude": float(zone.longitude),
        }
    else:
        zone_data = None

    return render(
        request,
        "shop/trace_product.html",
        {
            "product": product,
            "zone": zone_data,
        },
    )


@login_required
@require_POST
def add_to_cart(request, product_id):
    product = get_object_or_404(Product, id=product_id)
    try:
        quantity = int(request.POST.get("quantity", 1))
    except ValueError:
        quantity = 1
    quantity = max(1, quantity)

    item, created = CartItem.objects.get_or_create(user=request.user, product=product, defaults={"quantity": 0})
    next_qty = item.quantity + quantity
    if next_qty > product.stock:
        messages.error(request, "So luong vuot qua ton kho.")
        return redirect(request.META.get("HTTP_REFERER") or "shop:home")

    item.quantity = next_qty
    item.save(update_fields=["quantity"])
    messages.success(request, "Da them vao gio hang.")
    return redirect(request.META.get("HTTP_REFERER") or "shop:cart")


@login_required
def cart(request):
    summary = _calculate_cart_summary(request.user)
    return render(
        request,
        "shop/cart.html",
        {
            "cart_items": summary["cart_items"],
            "summary": summary,
        },
    )


@login_required
@require_POST
def update_cart(request, item_id):
    item = get_object_or_404(CartItem.objects.select_related("product"), id=item_id, user=request.user)
    try:
        quantity = int(request.POST.get("quantity", item.quantity))
    except ValueError:
        quantity = item.quantity

    if quantity <= 0:
        item.delete()
        messages.info(request, "Da xoa san pham khoi gio.")
        return redirect("shop:cart")

    if quantity > item.product.stock:
        messages.error(request, "So luong vuot qua ton kho.")
        return redirect("shop:cart")

    item.quantity = quantity
    item.save(update_fields=["quantity"])
    messages.success(request, "Da cap nhat gio hang.")
    return redirect("shop:cart")


@login_required
@require_POST
def remove_cart(request, item_id):
    item = get_object_or_404(CartItem, id=item_id, user=request.user)
    item.delete()
    messages.info(request, "Da xoa san pham.")
    return redirect("shop:cart")


@login_required
def account(request):
    addresses = Address.objects.filter(user=request.user)
    return render(request, "shop/account.html", {"addresses": addresses})


@login_required
@require_POST
def add_address(request):
    payload = {
        "recipient_name": request.POST.get("recipient_name", "").strip(),
        "phone": request.POST.get("phone", "").strip(),
        "street": request.POST.get("street", "").strip(),
        "ward": request.POST.get("ward", "").strip(),
        "district": request.POST.get("district", "").strip(),
        "city": request.POST.get("city", "").strip(),
    }
    if not all(payload.values()):
        messages.error(request, "Vui long nhap day du thong tin dia chi.")
        return redirect("shop:account")

    set_default = request.POST.get("set_default") == "on"
    if set_default or not Address.objects.filter(user=request.user).exists():
        Address.objects.filter(user=request.user).update(is_default=False)
        set_default = True

    Address.objects.create(user=request.user, is_default=set_default, **payload)
    messages.success(request, "Da them dia chi giao hang.")
    return redirect("shop:account")


@login_required
@require_POST
def set_default_address(request, address_id):
    address = get_object_or_404(Address, id=address_id, user=request.user)
    Address.objects.filter(user=request.user).update(is_default=False)
    address.is_default = True
    address.save(update_fields=["is_default"])
    messages.success(request, "Da cap nhat dia chi mac dinh.")
    return redirect("shop:account")


@login_required
def checkout(request):
    addresses = Address.objects.filter(user=request.user)
    if not addresses.exists():
        messages.error(request, "Ban can them dia chi truoc khi dat hang.")
        return redirect("shop:account")

    promo_code = request.POST.get("promo_code", request.GET.get("promo_code", "")).strip().upper()
    summary = _calculate_cart_summary(request.user, promo_code=promo_code)
    cart_items = summary["cart_items"]
    selected_payment_method = request.POST.get("payment_method", request.GET.get("payment_method", "")).strip()
    if selected_payment_method not in PAYMENT_METHOD_VALUES:
        selected_payment_method = PAYMENT_METHOD_COD
    if not cart_items:
        messages.warning(request, "Gio hang dang trong.")
        return redirect("shop:home")

    if request.method == "POST":
        address_id = request.POST.get("address_id")
        payment_method = request.POST.get("payment_method", PAYMENT_METHOD_COD)

        address = Address.objects.filter(user=request.user, id=address_id).first()
        if address is None:
            messages.error(request, "Dia chi giao hang khong hop le.")
            return redirect("shop:checkout")
        if payment_method not in PAYMENT_METHOD_VALUES:
            messages.error(request, "Phuong thuc thanh toan khong hop le.")
            return redirect("shop:checkout")

        for item in cart_items:
            if item.quantity > item.product.stock:
                messages.error(request, f"San pham {item.product.name} khong du ton kho.")
                return redirect("shop:cart")

        with transaction.atomic():
            order = Order.objects.create(
                user=request.user,
                address=address,
                status=Order.STATUS_PENDING,
                payment_method=payment_method,
                promo_code=summary["promotion"].code if summary["promotion"] else "",
                total_amount=summary["subtotal"],
                discount_amount=summary["discount"],
                final_amount=summary["total"],
            )
            _get_or_create_order_trace_token(order)

            for item in cart_items:
                zone = item.product.source_zone
                OrderItem.objects.create(
                    order=order,
                    product=item.product,
                    product_name=item.product.name,
                    unit_price=item.product.price,
                    quantity=item.quantity,
                    subtotal=item.product.price * item.quantity,
                    source_zone_name=zone.name if zone else "",
                    source_zone_code=zone.code if zone else "",
                    source_zone_province=zone.province if zone else "",
                    source_zone_latitude=zone.latitude if zone else None,
                    source_zone_longitude=zone.longitude if zone else None,
                )
                item.product.stock -= item.quantity
                item.product.save(update_fields=["stock"])

            CartItem.objects.filter(user=request.user).delete()

        messages.success(request, f"Dat hang thanh cong. Ma don #{order.id}")
        return redirect("shop:orders")

    return render(
        request,
        "shop/checkout.html",
        {
            "addresses": addresses,
            "payment_methods": PAYMENT_METHODS,
            "selected_payment_method": selected_payment_method,
            "bank_transfer": _build_bank_transfer_info(summary["total"], request.user.username),
            "cart_items": cart_items,
            "summary": summary,
            "promo_code": promo_code,
        },
    )


@login_required
def orders(request):
    user_orders = list(
        Order.objects.filter(user=request.user).select_related("address").prefetch_related("items")
    )
    for order in user_orders:
        _get_or_create_order_trace_token(order)
    return render(request, "shop/orders.html", {"orders": user_orders})


@login_required
@require_POST
def cancel_order(request, order_id):
    order = get_object_or_404(Order.objects.prefetch_related("items"), id=order_id, user=request.user)
    if order.status != Order.STATUS_PENDING:
        messages.error(request, "Chi co the huy don dang Pending.")
        return redirect("shop:orders")

    with transaction.atomic():
        order.status = Order.STATUS_CANCELLED
        order.save(update_fields=["status"])
        for item in order.items.all():
            if item.product:
                item.product.stock += item.quantity
                item.product.save(update_fields=["stock"])

    messages.success(request, "Da huy don hang.")
    return redirect("shop:orders")


def _collect_trace_zones(order):
    zones = []
    seen = set()

    for item in order.items.all():
        zone_name = item.source_zone_name
        zone_code = item.source_zone_code
        zone_province = item.source_zone_province
        zone_lat = item.source_zone_latitude
        zone_lng = item.source_zone_longitude

        if (not zone_name or zone_lat is None or zone_lng is None) and item.product and item.product.source_zone:
            source_zone = item.product.source_zone
            zone_name = source_zone.name
            zone_code = source_zone.code
            zone_province = source_zone.province
            zone_lat = source_zone.latitude
            zone_lng = source_zone.longitude

        if not zone_name or zone_lat is None or zone_lng is None:
            continue

        marker_key = f"{zone_name}-{zone_lat}-{zone_lng}"
        if marker_key in seen:
            continue
        seen.add(marker_key)
        zones.append(
            {
                "name": zone_name,
                "code": zone_code,
                "province": zone_province,
                "latitude": float(zone_lat),
                "longitude": float(zone_lng),
            }
        )
    return zones


@login_required
@require_GET
def order_trace_qr(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not request.user.is_staff and order.user_id != request.user.id:
        return HttpResponse(status=403)

    trace_url = _build_order_trace_url(request, order)
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(trace_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#1d3b2c", back_color="white")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return HttpResponse(buffer.getvalue(), content_type="image/png")


@require_GET
def trace_order(request, token):
    trace = get_object_or_404(
        OrderTraceToken.objects.select_related("order").prefetch_related("order__items__product__source_zone"),
        token=token,
    )
    order = trace.order
    zones = _collect_trace_zones(order)

    if zones:
        center_lat = sum(zone["latitude"] for zone in zones) / len(zones)
        center_lng = sum(zone["longitude"] for zone in zones) / len(zones)
    else:
        center_lat = 21.027763
        center_lng = 105.834160

    context = {
        "order": order,
        "trace": trace,
        "zones": zones,
        "zones_json": json.dumps(zones),
        "center_lat": center_lat,
        "center_lng": center_lng,
    }
    return render(request, "shop/trace_order.html", context)


@login_required
def chat_view(request):
    session_obj = _get_or_create_chat_session(request.user)
    history = session_obj.messages.exclude(role=ChatMessage.ROLE_SYSTEM)
    has_llm = bool(
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    chat_mode = "llm" if has_llm else "fallback"
    return render(
        request,
        "shop/chat.html",
        {
            "chat_history": history,
            "quick_replies": quick_replies(),
            "chat_mode": chat_mode,
        },
    )


@login_required
@require_POST
def chat_api(request):
    message = request.POST.get("message", "").strip()
    if not message:
        return JsonResponse({"response": "Ban hay nhap noi dung can ho tro.", "mode": "validation"}, status=400)

    session_obj = _get_or_create_chat_session(request.user)
    conversation = list(
        session_obj.messages.exclude(role=ChatMessage.ROLE_SYSTEM).values("role", "content")
    )

    ChatMessage.objects.create(session=session_obj, role=ChatMessage.ROLE_USER, content=message)
    response, mode = generate_chat_reply(request.user, conversation, message)
    ChatMessage.objects.create(session=session_obj, role=ChatMessage.ROLE_ASSISTANT, content=response)

    if session_obj.title == "Tro chuyen ho tro" and message:
        session_obj.title = message[:80]
        session_obj.save(update_fields=["title", "updated_at"])

    return JsonResponse(
        {
            "response": response,
            "mode": mode,
            "timestamp": timezone.localtime().strftime("%H:%M"),
        }
    )


@login_required
@require_POST
def chat_reset(request):
    ChatSession.objects.filter(user=request.user, is_active=True).update(is_active=False)
    ChatSession.objects.create(user=request.user, title="Tro chuyen ho tro", is_active=True)
    return JsonResponse({"ok": True})


def _get_or_create_chat_session(user):
    session_obj = ChatSession.objects.filter(user=user, is_active=True).first()
    if session_obj:
        return session_obj
    return ChatSession.objects.create(user=user, title="Tro chuyen ho tro", is_active=True)


def _normalize_dashboard_period(raw_period):
    period_key = (raw_period or "").strip().lower()
    if period_key not in DASHBOARD_PERIODS:
        period_key = "30d"
    return period_key, DASHBOARD_PERIODS[period_key]


def _build_change_info(current_value, previous_value):
    if previous_value == 0:
        if current_value == 0:
            return {
                "direction": "flat",
                "display": "0%",
                "note": "Khong doi so voi ky truoc",
            }
        return {
            "direction": "up",
            "display": "Moi",
            "note": "Ky truoc chua co du lieu",
        }

    delta = current_value - previous_value
    pct = (delta / previous_value) * Decimal("100")
    if pct > 0:
        direction = "up"
    elif pct < 0:
        direction = "down"
    else:
        direction = "flat"
    return {
        "direction": direction,
        "display": f"{pct:+.1f}%",
        "note": "So voi ky truoc",
    }


def _format_dashboard_bucket_label(bucket_value, bucket_type):
    value = bucket_value.date() if hasattr(bucket_value, "date") else bucket_value
    if bucket_type == "day":
        return value.strftime("%d/%m")
    if bucket_type == "week":
        iso_year, iso_week, _ = value.isocalendar()
        return f"Tuan {iso_week:02d}/{iso_year}"
    return value.strftime("%m/%Y")


def _is_staff_user(user):
    return user.is_authenticated and user.is_staff


@user_passes_test(_is_staff_user, login_url="shop:login")
def admin_dashboard(request):
    period_key, period_config = _normalize_dashboard_period(request.GET.get("period"))
    period_days = period_config["days"]
    bucket_type = period_config["bucket"]
    today = timezone.localdate()
    start_date = today - timedelta(days=period_days - 1)
    previous_start = start_date - timedelta(days=period_days)
    previous_end = start_date - timedelta(days=1)

    revenue_orders = Order.objects.filter(status__in=REVENUE_STATUSES)
    period_orders = revenue_orders.filter(created_at__date__range=(start_date, today))
    previous_orders = revenue_orders.filter(created_at__date__range=(previous_start, previous_end))

    period_revenue = period_orders.aggregate(value=Sum("final_amount")).get("value") or Decimal("0")
    previous_revenue = previous_orders.aggregate(value=Sum("final_amount")).get("value") or Decimal("0")
    period_order_count = period_orders.count()
    previous_order_count = previous_orders.count()
    average_order_value = period_revenue / period_order_count if period_order_count else Decimal("0")

    if bucket_type == "day":
        bucket_expr = TruncDate("created_at")
    elif bucket_type == "week":
        bucket_expr = TruncWeek("created_at")
    else:
        bucket_expr = TruncMonth("created_at")

    trend_rows = list(
        period_orders.annotate(bucket=bucket_expr)
        .values("bucket")
        .annotate(revenue=Sum("final_amount"), order_count=Count("id"))
        .order_by("bucket")
    )
    max_revenue = max((row["revenue"] or Decimal("0") for row in trend_rows), default=Decimal("0"))
    trend_series = []
    for row in trend_rows:
        revenue_value = row["revenue"] or Decimal("0")
        trend_series.append(
            {
                "label": _format_dashboard_bucket_label(row["bucket"], bucket_type),
                "revenue": revenue_value,
                "order_count": row["order_count"] or 0,
                "revenue_pct": int((revenue_value / max_revenue) * 100) if max_revenue else 0,
            }
        )

    top_products = list(
        OrderItem.objects.filter(
            order__status__in=REVENUE_STATUSES,
            order__created_at__date__range=(start_date, today),
        )
        .values("product_name")
        .annotate(quantity_sold=Sum("quantity"), revenue=Sum("subtotal"))
        .order_by("-quantity_sold", "-revenue")[:5]
    )

    trend_insights = []
    revenue_change = _build_change_info(period_revenue, previous_revenue)
    order_change = _build_change_info(Decimal(period_order_count), Decimal(previous_order_count))
    if period_order_count == 0:
        trend_insights.append("Chua co don hang hop le trong ky da chon.")
    elif revenue_change["direction"] == "up":
        trend_insights.append(f"Doanh thu dang tang ({revenue_change['display']}) {revenue_change['note'].lower()}.")
    elif revenue_change["direction"] == "down":
        trend_insights.append(f"Doanh thu dang giam ({revenue_change['display']}) {revenue_change['note'].lower()}.")
    else:
        trend_insights.append("Doanh thu dang on dinh, chua thay doi ro net.")

    if top_products:
        lead_product = top_products[0]
        trend_insights.append(
            f"San pham dan dau: {lead_product['product_name']} ({lead_product['quantity_sold']} san pham)."
        )
    if trend_series:
        peak = max(trend_series, key=lambda row: row["revenue"])
        trend_insights.append(
            f"Giai doan cao diem: {peak['label']} dat {peak['revenue']:.0f} VND."
        )

    stats = {
        "total_users": User.objects.count(),
        "total_products": Product.objects.count(),
        "total_orders": Order.objects.count(),
        "total_revenue": (
            Order.objects.filter(status__in=REVENUE_STATUSES)
            .aggregate(value=Sum("final_amount"))
            .get("value")
            or Decimal("0")
        ),
    }
    latest_orders = Order.objects.select_related("user").order_by("-created_at")[:10]
    period_options = [{"value": key, "label": option["label"]} for key, option in DASHBOARD_PERIODS.items()]
    can_manage_users = request.user.is_superuser
    return render(
        request,
        "shop/admin_dashboard.html",
        {
            "stats": stats,
            "latest_orders": latest_orders,
            "period_options": period_options,
            "selected_period": period_key,
            "period_label": period_config["label"],
            "period_start": start_date,
            "period_end": today,
            "period_revenue": period_revenue,
            "period_order_count": period_order_count,
            "average_order_value": average_order_value,
            "revenue_change": revenue_change,
            "order_change": order_change,
            "trend_series": trend_series,
            "trend_insights": trend_insights,
            "top_products": top_products,
            "can_manage_users": can_manage_users,
            "staff_role_label": "Quan ly admin" if can_manage_users else "Nhan vien",
        },
    )


@user_passes_test(_is_staff_user, login_url="shop:login")
def admin_products(request):
    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        if action == "create":
            name = request.POST.get("name", "").strip()
            description = request.POST.get("description", "").strip()
            short_description = request.POST.get("short_description", "").strip()
            category_id = request.POST.get("category_id", "").strip()
            source_zone_id = request.POST.get("source_zone_id", "").strip()
            image_url = request.POST.get("image_url", "").strip()
            uploaded_image = request.FILES.get("image")
            map_link = _normalize_map_link(request.POST.get("map_link", ""))

            try:
                price = Decimal(request.POST.get("price", "0").strip() or "0")
                stock = int(request.POST.get("stock", "0").strip() or "0")
            except Exception:
                messages.error(request, "Gia hoac ton kho khong hop le.")
                return redirect("shop:admin_products")

            category = Category.objects.filter(id=category_id).first()
            zone = ProductionZone.objects.filter(id=source_zone_id).first() if source_zone_id else None
            if not category or not name:
                messages.error(request, "Can nhap ten san pham va danh muc.")
                return redirect("shop:admin_products")

            product = Product(
                name=name,
                description=description or short_description or "Dang cap nhat mo ta.",
                short_description=short_description,
                category=category,
                source_zone=zone,
                price=max(price, Decimal("0")),
                stock=max(stock, 0),
                image_url=image_url,
                map_link=map_link,
            )
            if uploaded_image:
                product.image = uploaded_image
            product.save()
            messages.success(request, f"Da tao san pham #{product.id}.")
            return redirect("shop:admin_products")

        if action.startswith("delete:"):
            product_id = action.split(":", 1)[1].strip()
            product = Product.objects.filter(id=product_id).first()
            if not product:
                messages.error(request, "Khong tim thay san pham.")
                return redirect("shop:admin_products")
            product_name = product.name
            product.delete()
            messages.success(request, f"Da xoa san pham: {product_name}.")
            return redirect("shop:admin_products")

        if action == "bulk_delete_selected":
            raw_selected_ids = request.POST.getlist("selected_product_ids")
            selected_ids = []
            seen_ids = set()
            for raw_id in raw_selected_ids:
                cleaned = (raw_id or "").strip()
                if not cleaned.isdigit():
                    continue
                numeric_id = int(cleaned)
                if numeric_id in seen_ids:
                    continue
                seen_ids.add(numeric_id)
                selected_ids.append(numeric_id)

            if not selected_ids:
                messages.warning(request, "Ban chua chon san pham nao de xoa.")
                return redirect("shop:admin_products")

            selected_qs = Product.objects.filter(id__in=selected_ids)
            selected_count = selected_qs.count()
            selected_qs.delete()
            if selected_count:
                messages.success(request, f"Da xoa {selected_count} san pham da chon.")
            else:
                messages.warning(request, "Khong co san pham hop le de xoa.")
            return redirect("shop:admin_products")

        if action == "bulk_update":
            raw_product_ids = request.POST.getlist("product_ids")
            product_ids = []
            seen_ids = set()
            for raw_id in raw_product_ids:
                cleaned = (raw_id or "").strip()
                if not cleaned.isdigit():
                    continue
                numeric_id = int(cleaned)
                if numeric_id in seen_ids:
                    continue
                seen_ids.add(numeric_id)
                product_ids.append(numeric_id)

            if not product_ids:
                messages.error(request, "Khong co san pham nao de cap nhat.")
                return redirect("shop:admin_products")

            products = {product.id: product for product in Product.objects.filter(id__in=product_ids)}
            updated_count = 0
            failed_rows = []

            for product_id in product_ids:
                product = products.get(product_id)
                if not product:
                    failed_rows.append(str(product_id))
                    continue

                prefix = f"product_{product_id}_"
                name = request.POST.get(f"{prefix}name", "").strip()
                description = request.POST.get(f"{prefix}description", "").strip()
                short_description = request.POST.get(f"{prefix}short_description", "").strip()
                category_id = request.POST.get(f"{prefix}category_id", "").strip()
                source_zone_id = request.POST.get(f"{prefix}source_zone_id", "").strip()
                image_url = request.POST.get(f"{prefix}image_url", "").strip()
                uploaded_image = request.FILES.get(f"{prefix}image")
                clear_uploaded_image = request.POST.get(f"{prefix}clear_image") == "on"
                map_link = _normalize_map_link(request.POST.get(f"{prefix}map_link", ""))

                try:
                    price = Decimal(request.POST.get(f"{prefix}price", "0").strip() or "0")
                    stock = int(request.POST.get(f"{prefix}stock", "0").strip() or "0")
                except Exception:
                    failed_rows.append(str(product_id))
                    continue

                category = Category.objects.filter(id=category_id).first()
                zone = ProductionZone.objects.filter(id=source_zone_id).first() if source_zone_id else None
                if not category or not name:
                    failed_rows.append(str(product_id))
                    continue

                product.name = name
                product.description = description or short_description or "Dang cap nhat mo ta."
                product.short_description = short_description
                product.category = category
                product.source_zone = zone
                product.price = max(price, Decimal("0"))
                product.stock = max(stock, 0)
                product.image_url = image_url
                product.map_link = map_link
                if clear_uploaded_image and product.image:
                    product.image.delete(save=False)
                    product.image = None
                if uploaded_image:
                    if product.image:
                        product.image.delete(save=False)
                    product.image = uploaded_image
                product.save()
                updated_count += 1

            if updated_count:
                messages.success(request, f"Da cap nhat dong loat {updated_count} san pham.")
            if failed_rows:
                preview = ", ".join(f"#{row_id}" for row_id in failed_rows[:8])
                suffix = "..." if len(failed_rows) > 8 else ""
                messages.warning(
                    request,
                    f"Mot so dong khong hop le, bo qua: {preview}{suffix}.",
                )
            if not updated_count and not failed_rows:
                messages.info(request, "Khong co thay doi nao duoc ap dung.")
            return redirect("shop:admin_products")

        if action == "update":
            product_id = request.POST.get("product_id", "").strip()
            product = Product.objects.filter(id=product_id).first()
            if not product:
                messages.error(request, "Khong tim thay san pham.")
                return redirect("shop:admin_products")

            name = request.POST.get("name", "").strip()
            description = request.POST.get("description", "").strip()
            short_description = request.POST.get("short_description", "").strip()
            category_id = request.POST.get("category_id", "").strip()
            source_zone_id = request.POST.get("source_zone_id", "").strip()
            image_url = request.POST.get("image_url", "").strip()
            uploaded_image = request.FILES.get("image")
            clear_uploaded_image = request.POST.get("clear_image") == "on"
            map_link = _normalize_map_link(request.POST.get("map_link", ""))

            try:
                price = Decimal(request.POST.get("price", "0").strip() or "0")
                stock = int(request.POST.get("stock", "0").strip() or "0")
            except Exception:
                messages.error(request, "Gia hoac ton kho khong hop le.")
                return redirect("shop:admin_products")

            category = Category.objects.filter(id=category_id).first()
            zone = ProductionZone.objects.filter(id=source_zone_id).first() if source_zone_id else None
            if not category or not name:
                messages.error(request, "Can nhap ten san pham va danh muc.")
                return redirect("shop:admin_products")

            product.name = name
            product.description = description or short_description or "Dang cap nhat mo ta."
            product.short_description = short_description
            product.category = category
            product.source_zone = zone
            product.price = max(price, Decimal("0"))
            product.stock = max(stock, 0)
            product.image_url = image_url
            product.map_link = map_link
            if clear_uploaded_image and product.image:
                product.image.delete(save=False)
                product.image = None
            if uploaded_image:
                if product.image:
                    product.image.delete(save=False)
                product.image = uploaded_image
            product.save()
            messages.success(request, f"Da cap nhat san pham #{product.id}.")
            return redirect("shop:admin_products")

        if action == "delete":
            product_id = request.POST.get("product_id", "").strip()
            product = Product.objects.filter(id=product_id).first()
            if not product:
                messages.error(request, "Khong tim thay san pham.")
                return redirect("shop:admin_products")
            product_name = product.name
            product.delete()
            messages.success(request, f"Da xoa san pham: {product_name}.")
            return redirect("shop:admin_products")

        messages.error(request, "Hanh dong khong hop le.")
        return redirect("shop:admin_products")

    products = Product.objects.select_related("category", "source_zone").all()
    categories = Category.objects.all()
    zones = ProductionZone.objects.all()
    return render(
        request,
        "shop/admin_products.html",
        {
            "products": products,
            "categories": categories,
            "zones": zones,
        },
    )


@user_passes_test(_is_staff_user, login_url="shop:login")
def admin_orders(request):
    orders_qs = list(Order.objects.select_related("user").all())
    for order in orders_qs:
        _get_or_create_order_trace_token(order)
    return render(request, "shop/admin_orders.html", {"orders": orders_qs})


@user_passes_test(_is_staff_user, login_url="shop:login")
def admin_users(request):
    users_qs = User.objects.all().order_by("-date_joined")
    return render(request, "shop/admin_users.html", {"users": users_qs})


@user_passes_test(_is_staff_user, login_url="shop:login")
def admin_promotions(request):
    promotions = Promotion.objects.all()
    return render(request, "shop/admin_promotions.html", {"promotions": promotions})
