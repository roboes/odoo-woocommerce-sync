#!/usr/bin/env bash
set -euo pipefail

odoo_version="${ODOO_VERSION:-18.0}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
network="woocommerce-test-$$"
postgres="${network}-db"
image="woocommerce-sync-tests:${odoo_version}"

trap 'docker rm -f "${postgres}" >/dev/null 2>&1 || true
      docker network rm "${network}" >/dev/null 2>&1 || true' EXIT

# Quoted heredoc: bash leaves $VARS alone; the version arrives via --build-arg.
# Re-run with --no-cache to refresh the OCA clones.
docker build -q -t "${image}" --build-arg "ODOO_VERSION=$odoo_version" -f - "${repo_root}" >/dev/null <<'DOCKERFILE'
ARG ODOO_VERSION

# Stage 1: Fetch OCA dependencies (keeps git out of the final image)
FROM alpine/git AS oca
ARG ODOO_VERSION
RUN for repo in queue server-tools; do \
        git clone --quiet --depth 1 --branch "$ODOO_VERSION" \
            "https://github.com/OCA/$repo.git" "/oca/$repo"; \
    done

# Stage 2: Odoo + test dependencies
FROM odoo:${ODOO_VERSION}
USER root
ENV PIP_BREAK_SYSTEM_PACKAGES=1
# odoo-test-helper is needed by OCA modules' tests, which Odoo imports even when filtered by --test-tags
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt odoo-test-helper
COPY --from=oca /oca /opt/oca
USER odoo
DOCKERFILE

docker network create "${network}" >/dev/null
docker run -d --name "${postgres}" --network "${network}" \
    -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo \
    postgres:16 >/dev/null

until docker exec "${postgres}" pg_isready -U odoo >/dev/null 2>&1; do
    sleep 1
done

# The odoo image entrypoint reads HOST/USER/PASSWORD and overrides --db_* flags
output="$(docker run --rm --network "$network" \
    -e HOST="${postgres}" -e USER=odoo -e PASSWORD=odoo \
    -v "${repo_root}/woocommerce_sync:/mnt/extra-addons/woocommerce_sync:ro" \
    "${image}" \
    --database sync_test --init woocommerce_sync \
    --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons,/opt/oca/queue,/opt/oca/server-tools \
    --test-enable --test-tags /woocommerce_sync --stop-after-init \
    2>&1 | tee /dev/stderr)"

# Require an explicit, nonempty, all-green summary line
if ! grep -Eq '\b0 failed, 0 error\(s\) of [1-9][0-9]* tests' <<<"$output"; then
    echo "Odoo tests failed or no test summary found." >&2
    exit 1
fi
echo "Odoo ${odoo_version} tests passed."