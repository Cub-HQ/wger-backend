# Immutable #83 server fork layer. Source is public and commit-pinned.
FROM alpine:3.22@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1 AS source
ARG WGER_COMMIT=135d8569a3eb27c9f0f74e865d56372421a61294
RUN wget -qO /tmp/wger.tar.gz "https://github.com/Cubatica/wger/archive/${WGER_COMMIT}.tar.gz" \
    && mkdir /src \
    && tar -xzf /tmp/wger.tar.gz --strip-components=1 -C /src

FROM docker.io/wger/server:2.7@sha256:1c5789b93bfe5eed0b7287255782d9177027b255de2b22b59f511a693a48db04
ARG WGER_COMMIT=135d8569a3eb27c9f0f74e865d56372421a61294
LABEL org.opencontainers.image.source="https://github.com/Cubatica/wger" \
      org.opencontainers.image.revision="${WGER_COMMIT}" \
      org.opencontainers.image.licenses="AGPL-3.0"
USER root
COPY --from=source --chown=wger:wger /src/wger /home/wger/src/wger
COPY --chown=wger:wger settings-main.py /home/wger/src/settings/main.py
USER wger
