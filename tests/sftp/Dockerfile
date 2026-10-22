FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --password '*' --shell /usr/sbin/nologin macipsftptest \
    && mkdir --parents /run/sshd /home/macipsftptest/.ssh /home/macipsftptest/repository \
    && chown --recursive macipsftptest:macipsftptest /home/macipsftptest/.ssh /home/macipsftptest/repository \
    && chmod 700 /home/macipsftptest/.ssh

COPY sshd_config /etc/ssh/sshd_config

CMD ["/usr/sbin/sshd", "-D", "-e"]
