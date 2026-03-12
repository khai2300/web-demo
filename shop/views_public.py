import difflib
import unicodedata
from io import BytesIO

import qrcode
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from .models import Category, Product
from .services.seed import ensure_seed_data
from .views_utils import build_public_url

NEWS_POSTS = [
    {
        "title": "Tra Non Tom Thai Nguyen - Tinh Hoa Tu Nhung Bup Che Non",
        "excerpt": (
            "Bai viet gioi thieu tra non tom tu bup che non hai sang som, "
            "che bien ti mi de giu huong thanh va hau ngot. "
            "Kem huong dan pha (nuoc ~80-85C, ngam 3-5 phut) "
            "va loi ich suc khoe nhu chong oxy hoa, ho tro giam can, tang tap trung."
        ),
        "date": "2024-08",
        "image": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjP6kGyA9yGplciySGtmdwEVKtEnoQXWgzuhOADpzaUHokLoVE8hvla9otJMXpQIkTDOhJ2bBno1YWP6G2Rai1bJizsJ6wP4q5cSFRcym7GypFEHsMWdQqzE3lyPppv-AgOH9jMQ1wvYFEZk6UDOyP7iYnmDIHfQJQwMttynHRn09IzEuvoVeMdpieolbs/w640-h434/tra-non-tom-thai-nguyen.jpg",
        "url": "https://www.chebupthainguyen.com/2024/08/tra-non-tom-thai-nguyen-ngon.html",
    },
    {
        "title": "Bi Quyet Chon Che Ngon Thai Nguyen",
        "excerpt": (
            "Bai viet neu dau hieu che ngon (mau nuoc vang xanh trong, mui thom tu nhien, "
            "vi chat diu ngot hau), quy trinh san xuat tu thu hai sang som den sao/len men/say kho. "
            "Co goi y chon mua theo nguon goc, la che, mui, va huong dan pha 80-85C trong 3-5 phut."
        ),
        "date": "2024-08",
        "image": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEi3M6ymxmRhmIRhnSli1qr2w8lu6dMKZVdpE_K068eCV6jv1xVZovET5Pv54d1At7BJ2nhGEyBKLKoGkApM9LkZJdD4tH2qagdBcDST73IXcDm_KRIJbUmC1GYy_nMNzpxGDix9er79Os8epKSI4O4rR8qD_XqU8yz4NkNYk0doYGg1wqmOZwYw39n9iK8/w640-h640/chethainguyen.jpg",
        "url": "https://www.chebupthainguyen.com/2024/08/bi-quyet-chon-che-ngon-thai-nguyen.html",
    },
    {
        "title": "Tai Sao Tra Tan Cuong Thai Nguyen Noi Tieng",
        "excerpt": (
            "Bai viet giai thich ly do tra Tan Cuong noi tieng: dieu kien tu nhien vung tra, "
            "che bien thu cong, huong vi dac trung va loi ich suc khoe "
            "(ho tro tim mach, tieu hoa, tang mien dich, giam cang thang)."
        ),
        "date": "2024-08",
        "image": "https://blogger.googleusercontent.com/img/b/R29vZ2xl/AVvXsEjYxul3CcmnPWvKr3pYVxS07_O8MJnYBBOQELONPctB9boJirwtjX4mHzrUboUwY9JNXP91KpQrtL18ZJJUwpEV2H-Iefl9xCMLtt03tKIedPf1wQJM1xw_WO60apWqskHa06DqHGC_hmyBSKs3fW8c0IYxr8DjnlyEIQDbzaFBkemVRnH6jDlYI6zbnF8/w640-h640/Tr%C3%A0%20T%C3%A2n%20c%C6%B0%C6%A1ng%20th%C3%A1i%20nguy%C3%AAn.jpg",
        "url": "https://www.chebupthainguyen.com/2024/08/httpswww.chebupthainguyen.comtai-sao-tra-tan-cuong-thai-nguyen-noi-tieng.html",
    },
]


def _normalize_text(text):
    normalized = unicodedata.normalize("NFD", text or "")
    stripped = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return stripped.lower()


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
        normalized_q = _normalize_text(q)
        if normalized_q:
            matched = []
            for product in products.select_related("category"):
                haystack = f"{product.name} {product.description} {product.category.name}"
                if normalized_q in _normalize_text(haystack):
                    matched.append(product)
            products = matched
        else:
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

    normalized_q = _normalize_text(q)
    names = list(Product.objects.values_list("name", flat=True))
    contains = [name for name in names if normalized_q in _normalize_text(name)]
    fuzzy = difflib.get_close_matches(q, names, n=8, cutoff=0.3)
    merged = []
    for item in contains + fuzzy:
        if item not in merged:
            merged.append(item)
    return JsonResponse(merged[:8], safe=False)


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
    trace_url = build_public_url(request, trace_path)
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


@require_GET
def news_list(request):
    return render(request, "shop/news.html", {"posts": NEWS_POSTS})
