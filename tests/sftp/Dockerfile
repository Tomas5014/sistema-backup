FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin macipsftptest \
    && passwd --delete macipsftptest \
    && mkdir --parents /run/sshd /home/macipsftptest/.ssh /home/macipsftptest/repository \
    && chown --recursive macipsftptest:macipsftptest /home/macipsftptest/.ssh /home/macipsftptest/repository \
    && chmod 700 /home/macipsftptest/.ssh

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/macip-sftp-entrypoint

RUN chmod 755 /usr/local/bin/macip-sftp-entrypoint

ENTRYPOINT ["/usr/local/bin/macip-sftp-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
