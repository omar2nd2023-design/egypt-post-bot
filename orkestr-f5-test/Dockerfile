# orkestr-f5-test — Chromium + Playwright لإثبات المرور من F5 فقط.
# مفيش credentials ولا login — بيفتح صفحتين عامّتين ويتأكد إن فورم
# دخول Keycloak ظهر.
#
# الصورة قائمة بذاتها: بتنسخ probe.py بس، ومش محتاجة أي ملف من برّه
# مجلد orkestr-f5-test/.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PW_VERSION=1.48.0 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# playwright install --with-deps بيجيب حزم النظام اللي Chromium محتاجها
# (خطوط، مكتبات رسم، إلخ). --no-install-recommends بيقلّل حجم الصورة.
RUN pip install "playwright==${PW_VERSION}" \
 && playwright install --with-deps chromium \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /root/.cache

COPY probe.py .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "-u", "probe.py"]
