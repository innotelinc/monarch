# ClipBucket v5 bundled runtime - nginx + PHP-FPM + MariaDB in one container.
# Vendored from the upstream ClipBucket v5 Docker build and trimmed to the
# Monarch stack's needs (no phpMyAdmin / Xdebug / Redis / XHGui).
#
# The ClipBucket application source is git-cloned by entrypoint.sh on first
# boot when /srv/http/clipbucket is empty (persisted in the clipbucket_files
# volume), so this image only ships the runtime, never pinned app code.
#
# Multi-arch: debian, the sury PHP repository, ffmpeg and mariadb all ship
# linux/arm64 builds, so this image is built for amd64 + arm64 by the
# release workflow.

FROM debian:stable-slim

# Build arguments for customization
ARG INSTALL_MARIADB=true
ARG PHP_VERSION=8.5
ARG DOMAIN_NAME=clipbucket.local

# Environment variables for runtime
ENV DOMAIN_NAME=${DOMAIN_NAME}
ENV MYSQL_PASSWORD=clipbucket_password
ENV INSTALL_MARIADB=${INSTALL_MARIADB}
ENV PHP_VERSION=${PHP_VERSION}
ENV UID=1000
ENV GID=1000

# Install base dependencies and add the Ondrej Sury PHP repository (Debian
# stable only ships one PHP version; sury provides the full set for amd64
# and arm64).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg2 lsb-release nginx-full git \
    ffmpeg sendmail mediainfo wget unzip

RUN mkdir -p /etc/apt/keyrings \
    && curl -sSL https://packages.sury.org/php/apt.gpg -o /etc/apt/keyrings/sury-php.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/sury-php.gpg] https://packages.sury.org/php $(lsb_release -sc) main" > /etc/apt/sources.list.d/php.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        php${PHP_VERSION}-fpm \
        php${PHP_VERSION}-cli \
        php${PHP_VERSION}-dev \
        php${PHP_VERSION}-curl \
        php${PHP_VERSION}-mysqli \
        php${PHP_VERSION}-xml \
        php${PHP_VERSION}-mbstring \
        php${PHP_VERSION}-gd \
        php${PHP_VERSION}-zip \
        php${PHP_VERSION}-intl \
        php${PHP_VERSION}-fileinfo \
        php${PHP_VERSION}-tokenizer \
        php${PHP_VERSION}-ctype \
        php${PHP_VERSION}-iconv \
        php${PHP_VERSION}-simplexml \
        php${PHP_VERSION}-dom \
        php${PHP_VERSION}-sockets \
        php${PHP_VERSION}-posix \
        php${PHP_VERSION}-ffi

# Configure update-alternatives to use the correct PHP version
RUN update-alternatives --set php /usr/bin/php${PHP_VERSION} 2>/dev/null || \
    update-alternatives --install /usr/bin/php php /usr/bin/php${PHP_VERSION} 100

# Install MariaDB (bundled; the stack persists its data in the clipbucket_db
# volume at /var/lib/mysql).
RUN if [ "${INSTALL_MARIADB}" = "true" ]; then \
        apt-get install -y --no-install-recommends mariadb-server ; \
    fi

# PHP configuration (uploads/transcoding can run for a long time)
RUN sed -i "s/max_execution_time = 30/max_execution_time = 7200/g" /etc/php/${PHP_VERSION}/fpm/php.ini
RUN sed -i "s/;ffi.enable=preload/ffi.enable=true/g" /etc/php/${PHP_VERSION}/fpm/php.ini
RUN sed -i "s/;ffi.enable=preload/ffi.enable=true/g" /etc/php/${PHP_VERSION}/cli/php.ini

# Change the nginx and php-fpm user to the UID/GID account entrypoint creates
RUN sed -i 's/^user www-data;/user containeruser;/g' /etc/nginx/nginx.conf \
    && sed -i "s/^user = www-data\$/user = containeruser/" /etc/php/${PHP_VERSION}/fpm/pool.d/www.conf \
    && sed -i "s/^group = www-data\$/group = containeruser/" /etc/php/${PHP_VERSION}/fpm/pool.d/www.conf

# Copy nginx configuration and enable it
COPY nginx-clipbucket.conf /etc/nginx/sites-available/clipbucket
RUN rm -f /etc/nginx/sites-enabled/default && \
    ln -s /etc/nginx/sites-available/clipbucket /etc/nginx/sites-enabled/

# Replace placeholders in the nginx config
RUN sed -i "s/DOMAIN_NAME_PLACEHOLDER/${DOMAIN_NAME}/g" /etc/nginx/sites-available/clipbucket && \
    sed -i "s/phpPHP_VERSION_PLACEHOLDER-fpm/php${PHP_VERSION}-fpm/g" /etc/nginx/sites-available/clipbucket

# Entrypoint starts MariaDB + PHP-FPM + nginx and clones the app on first boot
COPY entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && \
    chmod +x /usr/local/bin/entrypoint.sh

# App sources volume (named clipbucket_files in the compose stack)
VOLUME ["/srv/http/clipbucket"]

EXPOSE 80

ENTRYPOINT ["entrypoint.sh"]