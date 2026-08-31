.PHONY: install install-dev run test redis-up redis-down

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

run:
	python bot.py

test:
	pytest -v

redis-up:
	docker run -d --name antalya-bot-redis -p 6379:6379 redis:7-alpine

redis-down:
	docker rm -f antalya-bot-redis
