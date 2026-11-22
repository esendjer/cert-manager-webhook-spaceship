# cert-manager-webhook-spaceship

- [cert-manager-webhook-spaceship](#cert-manager-webhook-spaceship)
  - [What it does](#what-it-does)
  - [Prerequisites](#prerequisites)
  - [1. Build and push the image](#1-build-and-push-the-image)
  - [2. Create the credentials secret](#2-create-the-credentials-secret)
  - [3. Deploy the webhook](#3-deploy-the-webhook)
  - [4. Create an issuer](#4-create-an-issuer)
  - [5. Request certificates](#5-request-certificates)
  - [6. Configuration](#6-configuration)
  - [7. Uninstall](#7-uninstall)
  - [8. Development](#8-development)
  - [9. Troubleshooting](#9-troubleshooting)
  - [10. License](#10-license)

A [cert-manager](https://cert-manager.io) DNS-01 webhook solver for
[Spaceship](https://www.spaceship.com) DNS, written with FastAPI.

## What it does

When cert-manager needs to prove control of a domain to an ACME CA (for example
Let's Encrypt) using the DNS-01 challenge, it calls this webhook. The webhook
uses the Spaceship DNS API to create the `_acme-challenge` TXT record, and
removes it again once the challenge is finished. This makes it possible to issue
certificates, including wildcard ones, for any domain hosted on Spaceship DNS.

```
cert-manager ──(Present / CleanUp)──▶ webhook ──(PUT / DELETE TXT)──▶ Spaceship DNS API
```

**Features**

- One deployment serves **every domain** in your Spaceship account: the zone to
  change is taken from each challenge request, not from configuration.
- Credentials are kept in a Kubernetes Secret, outside of the manifests.
- Cleanup is idempotent; challenge records are never overwritten, so several
  certificates can be validated at the same time.
- Served over TLS using a certificate issued by cert-manager itself.

## Prerequisites

- Kubernetes cluster with [cert-manager](https://cert-manager.io/docs/installation/) installed in the
  `cert-manager` namespace (including `cainjector`, which is part of the default install)
- A Spaceship [API key and secret](https://www.spaceship.com/application/api-manager/)
  with DNS record read/write permissions
- `kubectl` and `envsubst`
- A container image of this webhook (built from `Containerfile`) that the cluster can pull

## 1. Build and push the image

```sh
podman build -t registry.example.com/cert-manager-webhook-spaceship:1.0 -f Containerfile .
podman push registry.example.com/cert-manager-webhook-spaceship:1.0
```

## 2. Create the credentials secret

The secret is deliberately not part of the manifests:

```bash
read -r -s -p "Enter Spaceship API Key: " SPACESHIP_API_KEY
read -r -s -p "Enter Spaceship API Secret: " SPACESHIP_API_SECRET

kubectl create secret generic spaceship-credentials \
  --namespace cert-manager \
  --from-literal=SPACESHIP_API_KEY="$SPACESHIP_API_KEY" \
  --from-literal=SPACESHIP_API_SECRET="$SPACESHIP_API_SECRET"
```

## 3. Deploy the webhook

> **Optional:** you might want to install [kubernetes-reflector](https://github.com/emberstack/kubernetes-reflector) to enable automatic reflection of secrets across namespaces.
> 
>     kubectl -n kube-system apply -f https://github.com/emberstack/kubernetes-reflector/releases/latest/download/reflector.yaml

```sh
read -r -p "Enter Spaceship webhook image reference: " SPACESHIP_IMAGE_REF

envsubst '$SPACESHIP_IMAGE_REF' < deploy/webhook.yaml | kubectl apply -f -

kubectl -n cert-manager rollout status deploy/spaceship-webhook
```

This creates the ServiceAccount, RBAC, a private CA and serving certificate
(issued by cert-manager), the Deployment, the Service and the `APIService`
that registers the webhook with the Kubernetes API server.

Verify the API is registered and available:

```sh
kubectl get apiservice v1alpha1.dns01.spaceship.webhook
```

`AVAILABLE` should be `True`. The `dns01.spaceship.webhook` group name is only an
API identifier (the DNS-01 solver for Spaceship); it has nothing to do with the domains you manage and does not
need to be changed.

## 4. Create an issuer

Edit the e-mail in `deploy/examples.yaml` (and start with the Let's Encrypt
staging URL noted there), then:

```sh
kubectl apply -f deploy/examples.yaml
```

The relevant part is the solver, which any `Issuer`/`ClusterIssuer` can use:

```yaml
solvers:
  - dns01:
      webhook:
        groupName: dns01.spaceship.webhook
        solverName: spaceship
        config:
          ttl: 300   # optional, 60-3600
```

Credentials may alternatively be supplied per issuer via `config.apiKey` /
`config.apiSecret`, but this puts them in plain text in the issuer, so the
secret is preferred.

## 5. Request certificates

Use the issuer for any domain you own in Spaceship, including several at once:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: example
spec:
  secretName: example-tls
  dnsNames: [example.com, "*.example.com", other-domain.net]
  issuerRef: {name: letsencrypt-spaceship, kind: Issuer}
```

Check progress:

```sh
kubectl get certificate,order,challenge
kubectl -n cert-manager logs deploy/spaceship-webhook
```

## 6. Configuration

Environment variables of the webhook container:

| Variable | Default | Description |
|---|---|---|
| `SPACESHIP_API_KEY` | – | Spaceship API key |
| `SPACESHIP_API_SECRET` | – | Spaceship API secret |
| `SOLVER_NAME` | `spaceship` | Must match `solverName` in the issuer |
| `SPACESHIP_API_BASE` | `https://spaceship.dev/api/v1/dns/records` | API endpoint |
| `TLS_CERT_FILE` / `TLS_KEY_FILE` | `/tls/tls.crt` / `/tls/tls.key` | Serving certificate |

## 7. Uninstall

```sh
kubectl delete -f deploy/examples.yaml -f deploy/webhook.yaml
kubectl -n cert-manager delete secret spaceship-credentials
```

## 8. Development

```sh
uv sync
uv run pytest
```

The tests mock the Spaceship API, so no credentials or network access are needed.

## 9. Troubleshooting

- **APIService not available**: check the pod is ready and that the
  `spaceship-webhook-tls` certificate is `Ready`; `cainjector` must have filled
  in the APIService `caBundle`.
- **`forbidden` errors in cert-manager logs**: the ClusterRoleBinding for the
  `cert-manager` ServiceAccount is missing, or cert-manager runs in another
  namespace/ServiceAccount; adjust the binding subject.
- **Challenge stuck with "Spaceship API error"**: the message returned by
  Spaceship is shown in `kubectl describe challenge`.

## 10. License

[MIT](LICENSE)
