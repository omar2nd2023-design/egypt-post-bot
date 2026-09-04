# orkestr-f5-test — Chromium + Playwright لإثبات المرور من F5 فقط.
# مفيش credentials ولا login — بيفتح صفحتين عامّتين ويتأكد إن فورم
# دخول Keycloak ظهر.
#
# الصورة قائمة بذاتها: بتنسخ probe.py بس، ومش محتاجة أي ملف من برّه
# مجلد orkestr-f5-test/.
#
# ليه الصورة الرسمية بدل python:3.12-slim؟
#   البناء القديم كان بيقع عند `playwright install --with-deps chromium`
#   لأنه بيحاول يثبّت حزم Ubuntu 20.04 (ttf-unifont / ttf-ubuntu-font-family)
#   اللي مش موجودة في Debian Trixie بتاعة python:3.12-slim.
#   الصورة الرسمية جايّة بالمتصفحات ومكتبات النظام جاهزة — مفيش apt-get خالص.

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# المتصفحات مدمجة في الصورة، لكن حزمة playwright نفسها لأ —
# بنثبّتها بنفس النسخة (1.48.0) عشان تطابق المتصفحات المدمجة بالظبط.
RUN python3 -m pip install --no-cache-dir "playwright==1.48.0"

COPY probe.py .

ENV PORT=8000
EXPOSE 8000

CMD ["python3", "-u", "probe.py"]
