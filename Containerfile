# Disposable dev image for Lumina, built on AlmaLinux.
# Production deploys use the Ansible role, not this image.
FROM docker.io/library/almalinux:10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# python3.12 is the interpreter we target; mysqlclient needs the MariaDB
# connector headers and a C toolchain; nmap-ncat provides the `nc` binary
# the entrypoint uses to wait for MariaDB to accept connections.
RUN dnf install -y \
        python3.12 \
        python3.12-devel \
        python3.12-pip \
        gcc \
        pkgconf-pkg-config \
        mariadb-connector-c-devel \
        nmap-ncat \
        git \
    && dnf clean all

# Symlink python/pip to the 3.12 binaries so standard invocations work
# without callers having to remember the versioned name.
RUN ln -sf /usr/bin/python3.12 /usr/local/bin/python \
 && ln -sf /usr/bin/pip3.12 /usr/local/bin/pip

WORKDIR /app
# Editable install needs the source tree present; copy everything before
# installing. This sacrifices a tiny bit of layer caching in exchange for a
# simpler, more reliable image build.
COPY . .
RUN pip install --no-cache-dir -e '.[dev]'

COPY ops/devstack-entrypoint.sh /usr/local/bin/devstack-entrypoint
RUN chmod +x /usr/local/bin/devstack-entrypoint

ENTRYPOINT ["/usr/local/bin/devstack-entrypoint"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
