# اختبار egress بس — stdlib بايثون، مفيش أي dependency
FROM python:3.12-slim

WORKDIR /app
COPY server.py .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "-u", "server.py"]
