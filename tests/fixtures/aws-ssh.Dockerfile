# Synthetic transport verification only. Never use as an EC2/application image.
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server openssh-client python3 systemd && rm -rf /var/lib/apt/lists/*
