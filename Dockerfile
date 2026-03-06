FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn uvicorn

COPY . .

EXPOSE 7860
 
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "4", "--timeout", "120", "--worker-class", "uvicorn.workers.UvicornWorker", "app:demo"]