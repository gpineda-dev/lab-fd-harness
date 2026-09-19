# Sample 13: In-Band Deployment & Dynamic Stream Sanitization

Demonstrates real-time, in-band stream interception over `stdout` as presented in Act I of the blog post (*"L'Exosquelette du Bâtisseur & Le Superviseur de Flux"*).

---

## The Scenario

A shell script (`provision-worker.sh`) connects to a Kubernetes cluster and authenticates with sensitive session tokens (`sec_...`) and API keys (`sk_live_...`).

Without changing its execution environment, linking an external SDK, or generating intermediate files, the script instructs the supervisor to sanitize secrets on the fly simply by emitting structured comments to `stdout`:

```bash
echo '# @harness.filter:mask pattern="sec_[a-z0-9]{8}" action="alias" template="tok_{seq:02d}"'
echo '# @harness.filter:mask pattern="sk_live_[a-z0-9]{16}" action="hash" template="sk_{hash:8}"'
```

---

## How to Run

```bash
cd samples/13-deployment-inband
./run-demo.sh
```

### 1. Direct Execution (Without Supervisor)
The control directives are printed as raw text and the secrets leak in cleartext:
```text
==> [INIT] Démarrage du provisioning du cluster Kubernetes...
# @harness.filter:mask pattern="sec_[a-z0-9]{8}" action="alias" template="tok_{seq:02d}"
# @harness.filter:mask pattern="sk_live_[a-z0-9]{16}" action="hash" template="sk_{hash:8}"
==> [AUTH] Connexion au cluster primaire avec le secret : sec_8819ab21
==> [API]  Vérification de la licence avec l'API Key : sk_live_9948ab12cf345678
==> [AUTH] Renouvellement de session pour la clé secours : sec_4410cd99
==> [AUTH] Réutilisation du premier secret de session : sec_8819ab21
==> [DONE] Déploiement et provisioning terminés avec succès.
```

### 2. Execution Under `fd-harness run`
The supervisor intercepts and strips the directive lines from `stdout`, dynamically instantiates the DLP policies, and sanitizes the output in flight with 1:1 bijectivity:
```text
==> [INIT] Démarrage du provisioning du cluster Kubernetes...
==> [AUTH] Connexion au cluster primaire avec le secret : tok_01
==> [API]  Vérification de la licence avec l'API Key : sk_b286c581
==> [AUTH] Renouvellement de session pour la clé secours : tok_02
==> [AUTH] Réutilisation du premier secret de session : tok_01
==> [DONE] Déploiement et provisioning terminés avec succès.
```

