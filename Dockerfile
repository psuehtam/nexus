FROM node:22-slim AS frontend
WORKDIR /build/dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
RUN npm run build

FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alembic.ini .
COPY migrations ./migrations
COPY src ./src
COPY scripts ./scripts
COPY data/reviewed ./data/reviewed
COPY --from=frontend /build/dashboard/dist ./dashboard/dist
RUN mkdir -p /app/data/raw /app/data/staging /app/data/logs
EXPOSE 8000
CMD ["python","-m","uvicorn","src.api.main:app","--host","0.0.0.0","--port","8000","--workers","1"]
