.PHONY: up down logs collector enricher dashboard db

# Start all services in the background
up:
	docker-compose up -d --build

# Stop all services and remove containers
down:
	docker-compose down

# Tail all logs
logs:
	docker-compose logs -f

# Tail specific collector logs
collector:
	docker logs switchhitter-collector -f

# Tail specific enricher logs
enricher:
	docker logs switchhitter-enricher -f

# Tail specific dashboard logs
dashboard:
	docker logs switchhitter-dashboard -f

# Tail specific db logs
db:
	docker logs switchhitter-db -f
