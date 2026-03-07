FROM rust:1.80 as builder
WORKDIR /app
# Copy manifest and source
COPY Cargo.toml Cargo.lock ./
COPY src ./src
# Build release binary
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y libssl-dev ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Copy the compiled binary from builder
COPY --from=builder /app/target/release/engine .
# Copy necessary runtime files
COPY .env ./
COPY active_borrowers.json ./
# Run the engine
CMD ["./engine"]
