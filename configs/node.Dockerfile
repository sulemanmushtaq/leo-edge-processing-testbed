# Base image shared by all emulated nodes (site / sat1 / sat2 / ground).
# Kept lightweight; the inference stack (ultralytics, onnxruntime) is layered on
# top in a later step so network work doesn't require a multi-GB image.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 iperf3 iputils-ping procps ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/node_init.sh /usr/local/bin/node_init.sh
# Strip CRLF in case the file was edited on the Windows side.
RUN sed -i 's/\r$//' /usr/local/bin/node_init.sh && chmod +x /usr/local/bin/node_init.sh

WORKDIR /opt/leo
ENTRYPOINT ["/usr/local/bin/node_init.sh"]
CMD ["sleep", "infinity"]
