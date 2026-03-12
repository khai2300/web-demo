import json
from urllib.parse import urlencode
from io import BytesIO

import qrcode
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import Address, CartItem, Order, OrderItem, OrderTraceToken
from .views_utils import (
    PAYMENT_METHOD_COD,
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHODS,
    PAYMENT_METHOD_VALUES,
    build_bank_transfer_info,
    build_order_trace_url,
    calculate_cart_summary,
    get_or_create_order_trace_token,
)


@login_required
def checkout(request):
    addresses = Address.objects.filter(user=request.user)
    if not addresses.exists():
        messages.error(request, "Ban can them dia chi truoc khi dat hang.")
        return redirect("shop:account")

    promo_code = request.POST.get("promo_code", request.GET.get("promo_code", "")).strip().upper()
    summary = calculate_cart_summary(request.user, promo_code=promo_code)
    cart_items = summary["cart_items"]
    selected_payment_method = request.POST.get(
        "payment_method", request.GET.get("payment_method", "")
    ).strip()
    if selected_payment_method not in PAYMENT_METHOD_VALUES:
        selected_payment_method = PAYMENT_METHOD_COD
    if not cart_items:
        messages.warning(request, "Gio hang dang trong.")
        return redirect("shop:home")

    if request.method == "POST":
        address_id = request.POST.get("address_id")
        payment_method = request.POST.get("payment_method", PAYMENT_METHOD_COD)
        bank_transfer_name = request.POST.get("bank_transfer_name", "").strip()
        bank_transfer_phone = request.POST.get("bank_transfer_phone", "").strip()

        address = Address.objects.filter(user=request.user, id=address_id).first()
        if address is None:
            messages.error(request, "Dia chi giao hang khong hop le.")
            return redirect("shop:checkout")
        if payment_method not in PAYMENT_METHOD_VALUES:
            messages.error(request, "Phuong thuc thanh toan khong hop le.")
            return redirect("shop:checkout")
        if payment_method == PAYMENT_METHOD_BANK_TRANSFER and (
            not bank_transfer_name or not bank_transfer_phone
        ):
            messages.error(request, "Vui long nhap day du thong tin.")
            query = {"payment_method": payment_method}
            if promo_code:
                query["promo_code"] = promo_code
            return redirect(f"{reverse('shop:checkout')}?{urlencode(query)}")

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
            get_or_create_order_trace_token(order)

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
        success_url = reverse("shop:checkout_success")
        return redirect(f"{success_url}?order_id={order.id}")

    return render(
        request,
        "shop/checkout.html",
        {
            "addresses": addresses,
            "payment_methods": PAYMENT_METHODS,
            "selected_payment_method": selected_payment_method,
            "bank_transfer": build_bank_transfer_info(summary["total"], request.user.username),
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
        get_or_create_order_trace_token(order)
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

    trace_url = build_order_trace_url(request, order)
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
def checkout_success(request):
    order_id = request.GET.get("order_id", "").strip()
    order = None
    if order_id.isdigit():
        order = Order.objects.filter(id=int(order_id), user=request.user).first()
    return render(request, "shop/checkout_success.html", {"order": order})
