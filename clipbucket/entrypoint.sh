#!/bin/bash
set -e

# Set default PHP version if not specified
PHP_VERSION="${PHP_VERSION:-8.5}"

echo "========================================="
echo "ClipBucket v5 Docker Container"
echo "PHP Version: ${PHP_VERSION}"
echo "========================================="

if [ "${UID}" = "0" ]; then
    USER_NAME=root
else
    USER_NAME=containeruser

    if ! getent group ${GID} > /dev/null; then
      groupadd -g ${GID} ${USER_NAME}
    fi

    if ! getent passwd ${UID} > /dev/null; then
      useradd -m -u ${UID} -g ${GID} ${USER_NAME}
    fi
fi

# Adjust permissions for the new user
mkdir -p /srv/http/clipbucket /var/lib/nginx && chown -R ${USER_NAME}:${USER_NAME} /srv/http/clipbucket /run/php

if [ "${INSTALL_MARIADB}" = "true" ]; then
    mkdir -p /var/lib/mysql /run/mysqld && chown -R ${USER_NAME}:${USER_NAME} /var/lib/mysql /run/mysqld /usr/lib/mysql
fi

# Function to properly terminate child processes
terminate_processes() {
    echo "Terminating processes..."
    kill -TERM "${php_pid}" "${nginx_pid}" 2>/dev/null || true
    if [ "${INSTALL_MARIADB}" = "true" ]; then
        kill -TERM "${mariadb_pid}" 2>/dev/null || true
        wait "${mariadb_pid}" 2>/dev/null || true
    fi
    wait "${php_pid}" "${nginx_pid}" 2>/dev/null || true
    echo "All processes terminated."
    exit 1
}

# Capture signals to properly stop processes
trap terminate_processes SIGTERM SIGINT

# Mode with MariaDB
if [ "${INSTALL_MARIADB}" = "true" ]; then

    # Start MariaDB
    echo "Starting MariaDB..."
    mariadbd --user=${USER_NAME} --datadir=/var/lib/mysql &
    mariadb_pid=$!

    # Wait for the MariaDB socket to appear (20s)
    timeout=200
    elapsed=0
    while [ ! -e /var/run/mysqld/mysqld.sock ] && [ ${elapsed} -lt ${timeout} ]; do
      sleep 0.1
      elapsed=$((elapsed + 1))
    done

    if [ ! -e /var/run/mysqld/mysqld.sock ]; then
      echo "Error: MariaDB socket file not created after 20 seconds."
      exit 1
    fi

    # Check if the database exists
    if [ ! -d "/var/lib/mysql/clipbucket" ]; then
        echo "Init database..."
        mysql -uroot -e "CREATE DATABASE IF NOT EXISTS clipbucket;"
        mysql -uroot -e "CREATE USER IF NOT EXISTS 'clipbucket'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';"
        mysql -uroot -e "GRANT ALL PRIVILEGES ON clipbucket.* TO 'clipbucket'@'localhost';"
        mysql -uroot -e "FLUSH PRIVILEGES;"
    else
        echo "Database already exists. No init required."
    fi
else
    echo "MariaDB is disabled"
fi

# Start PHP-FPM
echo "Starting PHP-FPM ${PHP_VERSION}..."
php-fpm${PHP_VERSION} -F --fpm-config /etc/php/${PHP_VERSION}/fpm/php-fpm.conf --nodaemonize &
php_pid=$!

# Wait for the PHP-FPM socket to appear (20s)
timeout=200
elapsed=0
while [ ! -e /run/php/php${PHP_VERSION}-fpm.sock ] && [ ${elapsed} -lt ${timeout} ]; do
  sleep 0.1
  elapsed=$((elapsed + 1))
done

if [ ! -e /run/php/php${PHP_VERSION}-fpm.sock ]; then
  echo "Error: PHP-FPM socket file not created after 20 seconds."
  exit 1
fi

# Change socket file owner once available
chown ${USER_NAME}:${USER_NAME} /run/php/php${PHP_VERSION}-fpm.sock

# Check if the app sources exist (cloned on first boot, persisted in the
# clipbucket_files volume)
if [ ! "$(ls -A /srv/http/clipbucket)" ]; then
    echo "Init sources..."
    mkdir -p /srv/http/clipbucket
    git clone https://github.com/MacWarrior/clipbucket-v5.git /srv/http/clipbucket
    git config --global core.fileMode false
    git config --global --add safe.directory /srv/http/clipbucket
    chown -R ${USER_NAME}:${USER_NAME} /srv/http/clipbucket
    chmod 755 -R /srv/http/clipbucket
else
    echo "Sources already exist. No init required."
fi

# Start Nginx in foreground mode
echo "Starting Nginx..."
nginx -g "daemon off;" &
nginx_pid=$!

echo "========================================="
echo "All services started successfully!"
echo "========================================="

# Monitor processes and detect shutdowns
while true; do
    if [ "${INSTALL_MARIADB}" = "true" ]; then
        if ! kill -0 "${mariadb_pid}" 2>/dev/null; then
            echo "MariaDB process has exited. Exiting script..."
            terminate_processes
        fi
    fi

    if ! kill -0 "${php_pid}" 2>/dev/null; then
        echo "PHP-FPM process has exited. Exiting script..."
        terminate_processes
    fi

    if ! kill -0 "${nginx_pid}" 2>/dev/null; then
        echo "Nginx process has exited. Exiting script..."
        terminate_processes
    fi

    sleep 2
done