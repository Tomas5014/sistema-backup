FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin backup \
    && mkdir --parents /run/sshd /home/backup/.ssh /home/backup/repository \
    && chown --recursive backup:backup /home/backup/.ssh /home/backup/repository \
    && chmod 700 /home/backup/.ssh

COPY sshd_config /etc/ssh/sshd_config

CMD ["/usr/sbin/sshd", "-D", "-e"]
