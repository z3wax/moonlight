# PythonAnywhere'da ishga tushirish (Flask / WSGI)

Panel API'si **Web** bo'limida (WSGI, `flask_app.py`) ishlaydi, Telegram bot esa
alohida doimiy jarayon — **Always-on task** (`worker.py`). Ikkalasi bitta
`data/state.db` bazasi orqali gaplashadi.

> ⚠️ **Bepul (Beginner) akkauntda bot Telegram'ga ulana olmaydi**: bepul tarif
> internetga faqat ruxsat etilgan saytlarga HTTP orqali chiqadi, Always-on task
> ham yo'q. Panel ochiladi, lekin "Bot jarayoni ishlamayapti" deb turadi.
> Bot ishlashi uchun **Hacker** (pullik) tarif kerak.

Quyida `USERNAME` o'rniga PythonAnywhere foydalanuvchi nomingizni yozing.

## 1. Kodni yuklash (Consoles → Bash)

```bash
git clone https://github.com/z3wax/moonlight.git ~/moonlight
mkvirtualenv --python=python3.11 moonlight
pip install -r ~/moonlight/requirements.txt
```

## 2. `.env` fayl (Files → `/home/USERNAME/moonlight/.env`)

```
ADMIN_USERNAME=abu
ADMIN_PASSWORD=<panel paroli>
PROXY_SECRET=<Vercel'dagi PROXY_SECRET bilan bir xil>
SECRET_KEY=<uzun tasodifiy satr, keyin o'zgartirmang>
HF_TOKEN=<ixtiyoriy: MoonLight va zaxira uchun>
```

## 3. Web ilova (Web → Add a new web app)

1. **Manual configuration** → **Python 3.11**.
2. **Virtualenv**: `/home/USERNAME/.virtualenvs/moonlight`
3. **WSGI configuration file** ni oching, ichidagini o'chirib, quyidagini yozing:

```python
import sys
sys.path.insert(0, "/home/USERNAME/moonlight")
from flask_app import application
```

4. **Security** bo'limida **Force HTTPS** ni yoqing.
5. **Reload** tugmasini bosing.
6. Tekshirish: `https://USERNAME.pythonanywhere.com/healthz` → `{"ok": true, ...}`

## 4. Bot (Tasks → Always-on tasks)

Buyruq:

```
/home/USERNAME/.virtualenvs/moonlight/bin/python /home/USERNAME/moonlight/worker.py
```

`/healthz` endi `"worker": true` ko'rsatadi.

## 5. Vercel panel

Vercel'da `BACKEND_URL=https://USERNAME.pythonanywhere.com` qiling (yoki
panelni to'g'ridan-to'g'ri `https://USERNAME.pythonanywhere.com` dan oching —
buning uchun `.env` ga `ALLOW_DIRECT_PANEL=1` qo'shing).

## Yangilash

```bash
cd ~/moonlight && git pull && pip install -r requirements.txt
```

So'ng Web → **Reload** va Always-on task'ni qayta ishga tushiring.
