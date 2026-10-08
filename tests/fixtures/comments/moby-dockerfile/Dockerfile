# источник: moby v28.5.2, Dockerfile, строки 1–64
# syntax=docker/dockerfile:1

ARG GO_VERSION=1.24.9
ARG BASE_DEBIAN_DISTRO="bookworm"
ARG GOLANG_IMAGE="golang:${GO_VERSION}-${BASE_DEBIAN_DISTRO}"
ARG XX_VERSION=1.6.1

# VPNKIT_VERSION is the version of the vpnkit binary which is used as a fallback
# network driver for rootless.
ARG VPNKIT_VERSION=0.6.0

# DOCKERCLI_VERSION is the version of the CLI to install in the dev-container.
ARG DOCKERCLI_VERSION=v28.2.2
ARG DOCKERCLI_REPOSITORY="https://github.com/docker/cli.git"

# cli version used for integration-cli tests
ARG DOCKERCLI_INTEGRATION_REPOSITORY="https://github.com/docker/cli.git"
ARG DOCKERCLI_INTEGRATION_VERSION=v18.06.3-ce

# BUILDX_VERSION is the version of buildx to install in the dev container.
ARG BUILDX_VERSION=0.24.0

# COMPOSE_VERSION is the version of compose to install in the dev container.
ARG COMPOSE_VERSION=v2.36.2

ARG SYSTEMD="false"
ARG FIREWALLD="false"
ARG DOCKER_STATIC=1

# REGISTRY_VERSION specifies the version of the registry to download from
# https://hub.docker.com/r/distribution/distribution. This version of
# the registry is used to test schema 2 manifests. Generally,  the version
# specified here should match a current release.
ARG REGISTRY_VERSION=3.0.0

# delve is currently only supported on linux/amd64 and linux/arm64;
# https://github.com/go-delve/delve/blob/v1.24.1/pkg/proc/native/support_sentinel.go#L1
# https://github.com/go-delve/delve/blob/v1.24.1/pkg/proc/native/support_sentinel_linux.go#L1
#
# ppc64le support was added in v1.21.1, but is still experimental, and requires
# the "-tags exp.linuxppc64le" build-tag to be set:
# https://github.com/go-delve/delve/commit/71f12207175a1cc09668f856340d8a543c87dcca
ARG DELVE_SUPPORTED=${TARGETPLATFORM#linux/amd64} DELVE_SUPPORTED=${DELVE_SUPPORTED#linux/arm64} DELVE_SUPPORTED=${DELVE_SUPPORTED#linux/ppc64le}
ARG DELVE_SUPPORTED=${DELVE_SUPPORTED:+"unsupported"}
ARG DELVE_SUPPORTED=${DELVE_SUPPORTED:-"supported"}

# cross compilation helper
FROM --platform=$BUILDPLATFORM tonistiigi/xx:${XX_VERSION} AS xx

# dummy stage to make sure the image is built for deps that don't support some
# architectures
FROM --platform=$BUILDPLATFORM busybox AS build-dummy
RUN mkdir -p /build
FROM scratch AS binary-dummy
COPY --from=build-dummy /build /build

# base
FROM --platform=$BUILDPLATFORM ${GOLANG_IMAGE} AS base
COPY --from=xx / /
# Disable collecting local telemetry, as collected by Go and Delve;
#
# - https://github.com/go-delve/delve/blob/v1.24.1/CHANGELOG.md#1231-2024-09-23
# - https://go.dev/doc/telemetry#background
RUN go telemetry off && [ "$(go telemetry)" = "off" ] || { echo "Failed to disable Go telemetry"; exit 1; }
