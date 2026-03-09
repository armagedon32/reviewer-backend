FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ARG MONGO_TOOLS_VERSION=100.9.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://fastdl.mongodb.org/tools/db/mongodb-database-tools-linux-x86_64-${MONGO_TOOLS_VERSION}.tgz" -o /tmp/mongo-tools.tgz \
    && tar -xzf /tmp/mongo-tools.tgz -C /tmp \
    && cp /tmp/mongodb-database-tools-*/bin/mongodump /usr/local/bin/ \
    && cp /tmp/mongodb-database-tools-*/bin/mongorestore /usr/local/bin/ \
    && rm -rf /var/lib/apt/lists/* /tmp/mongo-tools.tgz /tmp/mongodb-database-tools-*

COPY requirements.txt /app/
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . /app

CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
