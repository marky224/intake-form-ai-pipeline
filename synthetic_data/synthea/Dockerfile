# Pinned Synthea wrapper. The JAR is checksum-verified at build time, so the
# image is reproducible byte-for-byte for a given Synthea release. Bump
# SYNTHEA_VERSION and SYNTHEA_JAR_SHA256 together when upgrading.
#
# Usage: see justfile `synthetic-data-patients` recipe.

FROM eclipse-temurin:17-jre-jammy

ARG SYNTHEA_VERSION=4.0.0
ARG SYNTHEA_JAR_SHA256=ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1

WORKDIR /opt/synthea

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o synthea.jar \
      "https://github.com/synthetichealth/synthea/releases/download/v${SYNTHEA_VERSION}/synthea-with-dependencies.jar" \
 && echo "${SYNTHEA_JAR_SHA256}  synthea.jar" | sha256sum -c - \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

# Output is bind-mounted by the host; the chmod is a safety net for
# the rare case someone runs without -v.
RUN mkdir -p /opt/synthea/output && chmod 0777 /opt/synthea/output

# years_of_history=1 keeps bundle sizes manageable. The CMS-1500 renderer
# only needs the most recent encounter; the default of 10 years pushes
# elderly-patient bundles past 50 MB and bloats checked-in fixtures. Override
# at runtime if a downstream consumer ever needs full history.
ENTRYPOINT ["java", "-jar", "/opt/synthea/synthea.jar", \
            "--exporter.baseDirectory=/opt/synthea/output", \
            "--exporter.fhir.export=true", \
            "--exporter.years_of_history=1"]

CMD ["-p", "10"]
