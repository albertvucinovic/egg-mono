FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    git \
    bash \
    && rm -rf /var/lib/apt/lists/*

COPY bin/applypatch /usr/bin/applypatch
COPY bin/apply_patch.py /usr/bin/apply_patch.py
COPY bin/apply_patch /usr/bin/apply_patch

RUN pip install numpy scipy pytest
RUN pip install rich requests readchar

WORKDIR /workspace
CMD ["bash"]
