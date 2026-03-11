import json
import os
import re
import urllib.error
import urllib.request
from decimal import Decimal

from django.utils import timezone

from shop.models import Address, Order, Product, Promotion


SYSTEM_PROMPT = (
    "Ban la tro ly ban che bup trong he thong ecommerce. "
    "Tra loi bang tieng Viet khong dau, than thien, ngan gon, khong may moc. "
    "Neu thong tin lien quan den don hang cua user da co trong context thi uu tien su dung dung du lieu do. "
    "Khong tu tao chinh sach khong co trong context. "
    "Neu user hoi ve san pham/tra, uu tien de xuat san pham tu he thong neu co."
)


def _format_money(value):
    amount = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    return f"{amount:,.0f} VND"


def _active_promotions():
    now = timezone.now()
    promos = []
    for promo in Promotion.objects.filter(is_active=True):
        valid_start = promo.start_at is None or promo.start_at <= now
        valid_end = promo.end_at is None or promo.end_at >= now
        if not (valid_start and valid_end):
            continue
        if promo.discount_type == Promotion.DISCOUNT_PERCENT:
            value = f"{promo.value:.0f}%"
        else:
            value = _format_money(promo.value)
        promos.append(f"{promo.code} ({value})")
    return promos[:5]


def _extract_order_id(text):
    if not text:
        return None
    match = re.search(r"(?:#|don\\s*|order\\s*)(\\d{1,8})", text.lower())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _order_status_text(status):
    mapping = {
        Order.STATUS_PENDING: "dang cho xac nhan",
        Order.STATUS_PROCESSING: "dang xu ly",
        Order.STATUS_SHIPPED: "dang giao",
        Order.STATUS_DELIVERED: "da giao",
        Order.STATUS_CANCELLED: "da huy",
    }
    return mapping.get(status, status)


def _recommend_products(user_message, limit=3):
    query = Product.objects.select_related("category").all()
    tokens = [token for token in re.findall(r"[a-z0-9]+", (user_message or "").lower()) if len(token) > 2]
    if not tokens:
        return list(query.order_by("-stock", "price")[:limit])

    scored = []
    for product in query:
        text = f"{product.name} {product.short_description} {product.description} {product.category.name}".lower()
        score = sum(1 for token in set(tokens) if token in text)
        scored.append((score, product.stock, -float(product.price), product))
    scored.sort(reverse=True, key=lambda item: (item[0], item[1], item[2]))
    selected = [item[3] for item in scored if item[0] > 0][:limit]
    if selected:
        return selected
    return list(query.order_by("-stock", "price")[:limit])


def _looks_like_product_query(message):
    text = (message or "").lower()
    keywords = ["goi y", "de xuat", "san pham", "che", "tra", "nen mua", "mua gi"]
    return any(word in text for word in keywords)


def _build_product_suggestions(message, limit=3):
    if not _looks_like_product_query(message):
        return [], []
    products = _recommend_products(message, limit=limit)
    lines = []
    for product in products:
        desc = (product.short_description or "").strip()
        desc_text = f" - {desc}" if desc else ""
        lines.append(
            f"- {product.name} ({_format_money(product.price)}){desc_text} | /product/{product.id}/"
        )
    return products, lines


def _build_user_context(user):
    latest_orders = (
        Order.objects.filter(user=user)
        .select_related("address")
        .order_by("-created_at")[:3]
    )
    default_address = Address.objects.filter(user=user, is_default=True).first()
    if default_address is None:
        default_address = Address.objects.filter(user=user).first()
    promotions = _active_promotions()

    lines = [f"Ten user: {user.username}"]
    if default_address:
        lines.append(f"Dia chi mac dinh: {default_address.full_address}")
    else:
        lines.append("Dia chi mac dinh: chua co")

    if latest_orders:
        for order in latest_orders:
            lines.append(
                f"Don #{order.id}: {_order_status_text(order.status)}, tong {_format_money(order.final_amount)}"
            )
    else:
        lines.append("Don hang: chua co don nao")

    if promotions:
        lines.append("Khuyen mai dang hoat dong: " + ", ".join(promotions))
    else:
        lines.append("Khuyen mai dang hoat dong: chua co")

    return "\n".join(lines)


def _rule_based_reply(user, user_message):
    message = (user_message or "").strip().lower()
    if not message:
        return "Ban cu nhan cau hoi, minh se ho tro ngay."

    if any(word in message for word in ["xin chao", "chao", "hello", "hi"]):
        latest = Order.objects.filter(user=user).order_by("-created_at").first()
        if latest:
            return (
                f"Chao {user.username}. Don gan nhat cua ban la #{latest.id}, "
                f"hien {_order_status_text(latest.status)}."
            )
        return f"Chao {user.username}. Ban can minh goi y che, kiem tra don hay ma giam gia?"

    if any(word in message for word in ["khuyen mai", "voucher", "ma giam", "giam gia"]):
        promos = _active_promotions()
        if promos:
            return "Hien shop dang co: " + ", ".join(promos) + ". Ban nhap ma o buoc checkout."
        return "Hien tai chua co ma giam gia dang hoat dong."

    if any(word in message for word in ["don hang", "trang thai", "kiem tra don", "order", "ma don"]):
        target_id = _extract_order_id(message)
        order_qs = Order.objects.filter(user=user)
        if target_id:
            order = order_qs.filter(id=target_id).first()
        else:
            order = order_qs.order_by("-created_at").first()
        if order:
            return (
                f"Don #{order.id} hien {_order_status_text(order.status)}. "
                f"Tong thanh toan {_format_money(order.final_amount)}."
            )
        return "Minh chua tim thay don phu hop. Ban thu gui ma don dang #123."

    if any(word in message for word in ["huy don", "huy"]):
        pending = Order.objects.filter(user=user, status=Order.STATUS_PENDING).order_by("-created_at")
        if pending.exists():
            ids = ", ".join(f"#{order.id}" for order in pending[:3])
            return (
                f"Ban dang co {pending.count()} don co the huy ({ids}). "
                "Vao trang Don hang va bam Huy don."
            )
        return "Hien tai ban khong co don Pending de huy."

    if any(word in message for word in ["giao hang", "ship", "van chuyen"]):
        shipping_order = (
            Order.objects.filter(user=user, status__in=[Order.STATUS_PROCESSING, Order.STATUS_SHIPPED])
            .order_by("-created_at")
            .first()
        )
        if shipping_order:
            return (
                f"Don #{shipping_order.id} dang {_order_status_text(shipping_order.status)}. "
                "Thuong mat 1-3 ngay lam viec tuy khu vuc."
            )
        return "Thoi gian giao thuong 1-3 ngay lam viec tuy khu vuc."

    if any(word in message for word in ["thanh toan", "payment", "cod", "bank", "vi"]):
        return (
            "Shop ho tro 2 cach thanh toan: COD va thanh toan online bang ngan hang (co ma QR). "
            "Neu ban muon nhan hang moi tra tien thi chon COD."
        )

    if any(word in message for word in ["dia chi", "address"]):
        count = Address.objects.filter(user=user).count()
        if count == 0:
            return "Ban chua co dia chi. Vao Tai khoan de them dia chi truoc khi checkout."
        return f"Ban dang co {count} dia chi giao hang. Ban co the dat 1 dia chi mac dinh."

    if any(word in message for word in ["goi y", "che", "tra", "san pham", "nen mua"]):
        products = _recommend_products(message, limit=3)
        if products:
            lines = []
            for product in products:
                desc = (product.short_description or "").strip()
                desc_text = f" - {desc}" if desc else ""
                lines.append(
                    f"- {product.name} ({_format_money(product.price)}){desc_text} | /product/{product.id}/"
                )
            return (
                "Minh goi y ban:\n"
                + "\n".join(lines)
                + "\nNeu can minh loc theo tam gia cu the."
            )
        return "Kho san pham dang cap nhat, ban thu lai sau it phut."

    if any(word in message for word in ["cam on", "thanks", "thank"]):
        return "Rat vui duoc ho tro ban. Can gi ban cu nhan minh ngay."

    return (
        "Minh da hieu y ban. Ban co the hoi tu nhien, vi du: "
        "'kiem tra don #12', 'goi y che thanh mat', 'co ma giam gia khong?'."
    )


def _call_openai(messages):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, "missing_api_key"

    is_groq_key = api_key.startswith("gsk_")
    default_endpoint = (
        "https://api.groq.com/openai/v1/chat/completions"
        if is_groq_key
        else "https://api.openai.com/v1/chat/completions"
    )
    default_model = "llama-3.3-70b-versatile" if is_groq_key else "gpt-4o-mini"

    endpoint = os.environ.get("OPENAI_CHAT_ENDPOINT", default_endpoint).strip()
    model = os.environ.get("OPENAI_CHAT_MODEL", default_model).strip()
    timeout = int(os.environ.get("OPENAI_CHAT_TIMEOUT", "25"))
    temperature = float(os.environ.get("OPENAI_CHAT_TEMPERATURE", "0.6"))

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 350,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except urllib.error.URLError:
        return None, "network_error"
    except TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "unknown_error"

    try:
        parsed = json.loads(raw)
        content = parsed["choices"][0]["message"]["content"]
        if not content:
            return None, "empty_response"
        provider = "groq" if "groq.com" in endpoint else "openai"
        return content.strip(), f"llm_{provider}"
    except Exception:
        return None, "parse_error"


def _call_gemini(system_text, conversation_messages, user_message):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return None, "missing_api_key"

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    endpoint = os.environ.get(
        "GEMINI_ENDPOINT",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    ).strip()
    timeout = int(os.environ.get("GEMINI_TIMEOUT", "25"))
    temperature = float(os.environ.get("GEMINI_TEMPERATURE", "0.6"))
    max_tokens_raw = os.environ.get("GEMINI_MAX_TOKENS", "").strip()

    contents = [{"role": "user", "parts": [{"text": system_text}]}]
    for item in conversation_messages[-12:]:
        role = "user" if item["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": item["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    payload = {
        "contents": contents,
        "generationConfig": {"temperature": temperature},
    }
    if max_tokens_raw:
        try:
            payload["generationConfig"]["maxOutputTokens"] = int(max_tokens_raw)
        except ValueError:
            pass

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except urllib.error.URLError:
        return None, "network_error"
    except TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "unknown_error"

    try:
        parsed = json.loads(raw)
        candidates = parsed.get("candidates", [])
        if not candidates:
            return None, "empty_response"
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        if not text:
            return None, "empty_response"
        return text.strip(), "llm_gemini"
    except Exception:
        return None, "parse_error"


def generate_chat_reply(user, conversation_messages, user_message):
    context_text = _build_user_context(user)
    products, product_lines = _build_product_suggestions(user_message, limit=3)

    system_text = f"{SYSTEM_PROMPT}\nContext user:\n{context_text}"
    if product_lines:
        system_text += "\nGoi y san pham tu he thong:\n" + "\n".join(product_lines)

    has_gemini = bool(
        os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    if has_gemini:
        llm_reply, mode = _call_gemini(system_text, conversation_messages, user_message)
    else:
        llm_messages = [{"role": "system", "content": system_text}]
        for item in conversation_messages[-12:]:
            llm_messages.append({"role": item["role"], "content": item["content"]})
        llm_messages.append({"role": "user", "content": user_message})
        llm_reply, mode = _call_openai(llm_messages)
    if llm_reply:
        if product_lines and _looks_like_product_query(user_message):
            reply_lower = llm_reply.lower()
            if not any(product.name.lower() in reply_lower for product in products):
                llm_reply = llm_reply + "\n\nGoi y san pham tu he thong:\n" + "\n".join(product_lines)
        return llm_reply, mode

    fallback = _rule_based_reply(user, user_message)
    return fallback, f"fallback_{mode}"


def quick_replies():
    return [
        "Kiem tra don hang gan nhat",
        "Co ma giam gia nao dang dung?",
        "Goi y 3 loai che bup de uong hang ngay",
        "Huong dan thanh toan bang COD",
    ]
