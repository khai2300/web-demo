from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Address


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
@require_POST
def delete_address(request, address_id):
    address = get_object_or_404(Address, id=address_id, user=request.user)
    was_default = address.is_default
    address.delete()

    if was_default:
        next_address = Address.objects.filter(user=request.user).order_by("-created_at").first()
        if next_address:
            Address.objects.filter(user=request.user).update(is_default=False)
            next_address.is_default = True
            next_address.save(update_fields=["is_default"])

    messages.success(request, "Da xoa dia chi giao hang.")
    return redirect("shop:account")
