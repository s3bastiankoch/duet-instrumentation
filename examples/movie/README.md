# Movie example app

## Run locally with Docker MongoDB

```bash
# start MongoDB
docker compose -f docker-compose.ext.yml up -d

# install deps and run app
bun install
export JWT_SECRET="dev-secret"
export MONGO_URL="mongodb://127.0.0.1:27017/test"
bun run dev
```

App listens on `http://localhost:4000`.

## Run app and MongoDB with Docker

```bash
export JWT_SECRET="dev-secret"
export MONGO_URL="mongodb://mongodb:27017/test"

docker compose -f docker-compose.yml -f docker-compose.ext.yml up --build
```

## Stop MongoDB (local run)

```bash
docker compose -f docker-compose.ext.yml down
```
