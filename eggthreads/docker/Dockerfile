FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    bash \
    && rm -rf /var/lib/apt/lists/*

RUN pip install numpy scipy

WORKDIR /workspace
CMD ["bash"]
