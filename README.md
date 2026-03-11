# Tea Shop Django

Ung dung web ban che bup su dung Django + Bootstrap + JavaScript.

## Chay nhanh

```powershell
cd c:\Users\admin\Documents\GitHub\bt
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Mo trinh duyet: `http://127.0.0.1:8000`

## Tai khoan admin mac dinh

- Username: `admin`
- Password: `admin123`

Neu chua co du lieu mau, trang chu se tu dong seed san pham khi truy cap lan dau.

## Thu muc giao dien

- `django_ui/templates/shop`
- `django_ui/static/shop`

## URL chinh

- `/` danh sach san pham
- `/register/`, `/login/`, `/logout/`
- `/cart/`, `/checkout/`
- `/account/`, `/orders/`
- `/trace/product/<product_id>/` truy xuat nguon tung san pham
- `/product/<product_id>/trace-qr.png` QR truy xuat san pham
- `/chat/`
- `/dashboard/admin/`

## Chat AI hoan chinh (LLM + fallback)

Chat se tu dong chay theo 2 che do:

- Co API key: goi LLM that (Gemini hoac Groq/OpenAI compatible), co nho ngu canh hoi thoai.
- Khong co API key: fllback thong minh dua tren du lieu don hang/san pham.

### Cau hinh Gemini (PowerShell)

```powershell
$env:GEMINI_API_KEY="YOUR_GEMINI_KEY"
$env:GEMINI_MODEL="gemini-2.5-flash"
python manage.py runserver
```
### Cau hinh Groq (PowerShell)

```powershell
$env:OPENAI_API_KEY="YOUR_GROQ_KEY"
$env:OPENAI_CHAT_ENDPOINT="https://api.groq.com/openai/v1/chat/completions"
$env:OPENAI_CHAT_MODEL="llama-3.3-70b-versatile"
python manage.py runserver
```

### Bien moi truong ho tro

- `GEMINI_API_KEY` (hoac `GOOGLE_API_KEY`)
- `GEMINI_MODEL` (optional, mac dinh `gemini-2.5-flash`)
- `GEMINI_ENDPOINT` (optional)
- `GEMINI_TIMEOUT` (optional, mac dinh `25`)
- `GEMINI_TEMPERATURE` (optional, mac dinh `0.6`)
- `GEMINI_MAX_TOKENS` (optional)
- `OPENAI_API_KEY`
- `OPENAI_CHAT_ENDPOINT` (optional)
- `OPENAI_CHAT_MODEL` (optional)
- `OPENAI_CHAT_TIMEOUT` (optional, mac dinh `25`)
- `OPENAI_CHAT_TEMPERATURE` (optional, mac dinh `0.6`)

## Upload anh tu thu muc may tinh

- Vao: `/dashboard/admin/products/`
- O form "Them san pham moi", chon file trong o `Anh` de upload tu may tinh.
- Co the cap nhat anh nhanh tung san pham ngay trong bang danh sach.
