# Guida al deploy della demo: Azure Container Apps + Vercel

Questa guida spiega come pubblicare online la demo del progetto TCC:

- i **5 microservizi Python** su **Azure Container Apps** (ogni servizio è un container con un URL HTTPS pubblico);
- il **frontend statico** (`frontend/`) su **Vercel**.

Non serve solo copiare i comandi: per ogni passo trovi **cosa fa** e **perché serve**. Leggi la guida in ordine, perché alcuni passi dipendono dai precedenti.

---

## 0. Come è fatto il sistema (ripasso)

```text
 Browser (utente)
     |
     |  HTTPS, chiamate dirette dal JavaScript
     |
 Vercel  ──►  frontend/index.html + script.js + style.css   (file statici)
     │
     ├──► auth-service       (JWT)           Azure Container App
     ├──► order-service      (coordinatore)  Azure Container App ──┐
     ├──► inventory-service  (partecipante)  Azure Container App ◄─┤  chiamate
     ├──► payment-service    (partecipante)  Azure Container App ◄─┤  interne
     └──► shipping-service   (partecipante)  Azure Container App ◄─┘
                  │
                  └──► Stripe (sandbox, solo da payment-service)
```

Punti chiave da capire:

- **Il browser chiama i servizi direttamente**, non passa da un backend unico. Per questo ogni servizio deve avere un URL pubblico raggiungibile dal browser.
- **Il coordinatore (`order-service`) chiama gli altri servizi**. Quindi deve conoscere i loro URL, che Azure assegna solo dopo la creazione.
- **Il segreto Stripe sta solo nel `payment-service`**. Il frontend non lo conosce mai, e non deve conoscerlo.

---

## 1. Concetti Azure che userai

| Concetto | Cos'è | Perché ti serve |
|---|---|---|
| **Resource group** | Contenitore logico di tutte le risorse Azure del progetto | Eliminando il gruppo elimini tutto in un colpo solo (utile per non pagare dopo la demo). |
| **Azure Container Registry (ACR)** | Un "magazzino" privato di immagini Docker | Azure non legge il tuo codice: legge immagini già pronte da un registro. |
| **Immagine Docker** | Il pacchetto eseguibile del servizio (codice + Python + dipendenze), descritto dal `Dockerfile` | È quello che Azure esegue. |
| **Container Apps environment** | La rete condivisa in cui girano i container, con il logging | I servizi devono stare nello stesso environment per potersi chiamare. |
| **Container App** | Un singolo servizio in esecuzione (es. `inventory-service`) | Una app per ciascun microservizio. |
| **Ingress** | La configurazione che rende raggiungibile un container dall'esterno | `external` = URL HTTPS pubblico. |
| **Secret** | Un valore sensibile salvato in Azure | Per `JWT_SECRET` e `STRIPE_SECRET_KEY`, senza scriverli nel codice. |
| **Variabili d'ambiente** | Valori di configurazione passati al container | Il codice le legge con `os.getenv(...)`. |
| **Min replicas** | Numero minimo di copie del container sempre attive | Con `1`, il servizio non si "spegne" e il primo accesso non è lento. |

---

## 2. Prerequisiti

1. **Account Azure con una sottoscrizione attiva.** Se non ce l'hai, puoi crearne uno gratuito.
2. **Azure CLI installata.** Verifica con:
   ```bash
   az version
   ```
3. **Account GitHub** con il repository del progetto (il remote è `origin` su GitHub). Serve per Vercel.
4. **Account Vercel** collegato a GitHub.
5. **Una chiave di test Stripe** (`sk_test_...`), dalla dashboard Stripe in modalità test.

### Accesso e preparazione della CLI

```bash
az login
az upgrade
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.ContainerRegistry
```

**Se `az extension add` dice `No stable version of 'containerapp' to install`:** non è un errore. Significa che non c'è una versione stabile e l'estensione installata è una *preview*, che funziona comunque. Puoi verificarla con `az extension list --query "[?name=='containerapp']" -o table`. Se preferisci installare la preview in modo esplicito, usa `az extension add --name containerapp --upgrade --allow-preview true`.

**Perché questi passi:**

- `az login` autentica la CLI con il tuo account Azure.
- `az upgrade` e l'estensione `containerapp` servono ad avere i comandi `az containerapp` aggiornati.
- La **registrazione dei provider** abilita, nella tua sottoscrizione, i servizi che useremo (Container Apps e il logging). Senza, i comandi possono fallire con errori del tipo "provider not registered". La registrazione è un'operazione una tantum.

---

## 3. Variabili da impostare una volta sola

Scegli i nomi e tienili in un file, così li ritrovi se chiudi il terminale. Le variabili della shell **non sopravvivono** alla chiusura del terminale.

```bash
RG=rg-tcc                 # resource group
LOC=francecentral         # regione consentita dalla policy della sottoscrizione. Elenco consentito: germanywestcentral, francecentral, austriaeast, polandcentral, norwayeast
ENV=tcc-env               # environment Container Apps
ACR=tccdemo12345          # nome ACR: solo lettere minuscole e numeri, UNICO in tutto Azure
```

**Perché il nome ACR deve essere unico:** il nome del registro diventa parte di un URL pubblico (`tccdemo12345.azurecr.io`). Se è già usato da qualcun altro, scegline un altro (aggiungi cifre).

---

## 4. Creare il resource group e il registro immagini

```bash
az group create --name $RG --location $LOC

az acr create --resource-group $RG --name $ACR --sku Basic
```

**Cosa fanno:**

- Il **resource group** raccoglie tutte le risorse, così sono gestite e cancellate insieme.
- L'**ACR** è il registro dove finiranno le immagini Docker dei servizi.
- Lo SKU `Basic` è il più economico, adatto alla demo.

Abilita l'accesso amministratore al registro (serve per far scaricare le immagini ad Azure Container Apps):

```bash
az acr update --name $ACR --admin-enabled true
```

**Perché:** Container Apps deve autenticarsi per scaricare le immagini dal tuo registro. L'account admin è la via più semplice per una demo. In un progetto reale si usa un'**identità gestita**, che non richiede password.

---

## 5. Costruire le immagini Docker

> **Alternativa se `az acr build` dà `TasksOperationsNotAllowed`:** alcune sottoscrizioni (come quelle *Azure for Students*) non possono usare ACR Tasks, cioè la build nel cloud. In quel caso costruisci le immagini in locale con Docker e caricale nel registro:
>
> ```bash
> az acr login --name $ACR
> for s in auth inventory payment shipping order; do
>   docker build --platform linux/amd64 -t $ACR.azurecr.io/$s:1 ./services/$s-service
>   docker push $ACR.azurecr.io/$s:1
> done
> ```
>
> `--platform linux/amd64` è necessario perché Azure Container Apps esegue immagini x86_64, anche se il tuo computer è un Mac con chip ARM.

Dalla **cartella radice del progetto** esegui, per ciascun servizio (metodo principale, con ACR Tasks):

```bash
az acr build --registry $ACR --image auth:1        ./services/auth-service
az acr build --registry $ACR --image inventory:1   ./services/inventory-service
az acr build --registry $ACR --image payment:1     ./services/payment-service
az acr build --registry $ACR --image shipping:1    ./services/shipping-service
az acr build --registry $ACR --image order:1       ./services/order-service
```

**Cosa succede:**

1. `az acr build` carica la cartella del servizio su Azure.
2. Azure esegue il `Dockerfile` (installa Python 3.12, installa le dipendenze di `requirements.txt`, copia `main.py`).
3. L'immagine risultante viene salvata nel registro con il nome `auth:1`, `inventory:1`, ecc.

**Perché serve questo passo:** non devi avere Docker installato in locale, la costruzione avviene nel cloud. Il tag `:1` è una versione: se modifichi il codice, ricostruisci con `:2` e aggiorni il container.

Verifica che le immagini ci siano:

```bash
az acr repository list --name $ACR --output table
```

---

## 6. Creare l'environment

```bash
az containerapp env create --name $ENV --resource-group $RG --location $LOC
```

**Perché:** crea la rete condivisa e il workspace di logging (Log Analytics). I log dei container compaiono qui, utili se qualcosa non funziona.

---

## 7. Recuperare le credenziali del registro

```bash
ACR_USER=$(az acr credential show --name $ACR --query username -o tsv)
ACR_PWD=$(az acr credential show --name $ACR --query "passwords[0].value" -o tsv)
```

**Perché:** queste due variabili servono ai comandi `create` per permettere a Container Apps di scaricare le immagini. Non le scrivere in file del progetto.

---

## 8. Creare i servizi partecipanti (inventory, payment, shipping)

Questi servizi non dipendono da altri, quindi li creiamo per primi. Il loro URL ci servirà dopo.

```bash
az containerapp create --name inventory-service --resource-group $RG --environment $ENV \
  --image $ACR.azurecr.io/inventory:1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PWD \
  --ingress external --target-port 8000 --min-replicas 1 \
  --env-vars TTL_SECONDS=60

az containerapp create --name shipping-service --resource-group $RG --environment $ENV \
  --image $ACR.azurecr.io/shipping:1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PWD \
  --ingress external --target-port 8000 --min-replicas 1 \
  --env-vars TTL_SECONDS=60
```

Per il `payment-service` serve anche la chiave Stripe. **Non scriverla direttamente nel comando**, perché resterebbe nella cronologia della shell. Inseriscila in modo nascosto:

```bash
read -s -p "Chiave Stripe test (sk_test_...): " STRIPE_KEY; echo

az containerapp create --name payment-service --resource-group $RG --environment $ENV \
  --image $ACR.azurecr.io/payment:1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PWD \
  --ingress external --target-port 8000 --min-replicas 1 \
  --secrets stripe-key="$STRIPE_KEY" \
  --env-vars TTL_SECONDS=60 STRIPE_SECRET_KEY=secretref:stripe-key STRIPE_CURRENCY=eur
```

**Spiegazione dei flag principali:**

- `--image` indica quale immagine eseguire, presa dal registro.
- `--registry-*` sono le credenziali per scaricarla.
- `--ingress external` rende il servizio raggiungibile da internet.
- `--target-port 8000` è la porta su cui uvicorn ascolta dentro il container (vedi `Dockerfile`: `--port 8000`).
- `--min-replicas 1` tiene sempre un'istanza attiva, quindi niente attesa al primo accesso.
- `--env-vars` imposta variabili normali. `STRIPE_SECRET_KEY=secretref:stripe-key` significa: "il valore è quello del secret chiamato `stripe-key`", senza mostrarlo in chiaro.
- `--secrets` salva il valore in modo cifrato.

Recupera gli URL pubblici dei tre servizi:

```bash
INV_URL=https://$(az containerapp show --name inventory-service --resource-group $RG --query properties.configuration.ingress.fqdn -o tsv)
SHIP_URL=https://$(az containerapp show --name shipping-service  --resource-group $RG --query properties.configuration.ingress.fqdn -o tsv)
PAY_URL=https://$(az containerapp show --name payment-service    --resource-group $RG --query properties.configuration.ingress.fqdn -o tsv)
echo "$INV_URL  $SHIP_URL  $PAY_URL"
```

**Perché così:** `fqdn` è il nome a dominio completo assegnato da Azure. Il prefisso `https://` lo rende un URL usabile. Questi URL servono per l'`order-service`.

---

## 9. Creare l'auth-service

Il segreto JWT deve essere **identico** in `auth-service` e `order-service`: uno emette il token, l'altro lo verifica.

```bash
read -s -p "JWT_SECRET (stessa stringa per auth e order): " JWT_SECRET; echo

az containerapp create --name auth-service --resource-group $RG --environment $ENV \
  --image $ACR.azurecr.io/auth:1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PWD \
  --ingress external --target-port 8000 --min-replicas 1 \
  --secrets jwt-secret="$JWT_SECRET" \
  --env-vars JWT_SECRET=secretref:jwt-secret

AUTH_URL=https://$(az containerapp show --name auth-service --resource-group $RG --query properties.configuration.ingress.fqdn -o tsv)
```

**Perché:** se i due segreti sono diversi, `order-service` rifiuterà i token emessi da `auth-service` e la demo non funzionerà, anche se tutto il resto è corretto.

---

## 10. Creare l'order-service (per ultimo)

Il coordinatore ha bisogno degli URL degli altri servizi, quindi viene creato dopo.

```bash
az containerapp create --name order-service --resource-group $RG --environment $ENV \
  --image $ACR.azurecr.io/order:1 \
  --registry-server $ACR.azurecr.io --registry-username $ACR_USER --registry-password $ACR_PWD \
  --ingress external --target-port 8000 --min-replicas 1 \
  --secrets jwt-secret="$JWT_SECRET" \
  --env-vars JWT_SECRET=secretref:jwt-secret \
    INVENTORY_SERVICE_URL=$INV_URL \
    PAYMENT_SERVICE_URL=$PAY_URL \
    SHIPPING_SERVICE_URL=$SHIP_URL \
    MAX_RETRY_ATTEMPTS=5 RETRY_DELAY_SECONDS=1 ORDERS_FILE=/data/orders.json

ORDER_URL=https://$(az containerapp show --name order-service --resource-group $RG --query properties.configuration.ingress.fqdn -o tsv)
```

**Attenzione:** nel `docker-compose.yml` i servizi comunicano con nomi interni (`http://inventory-service:8000`). Su Azure i nomi interni non funzionano allo stesso modo, quindi usiamo gli **URL pubblici HTTPS**. Questo funziona perché gli ingress sono `external` e CORS è aperto.

**Nota sulla persistenza:** `ORDERS_FILE=/data/orders.json` salva su un filesystem del container. Su Container Apps quel filesystem è effimero: se il container si riavvia, gli ordini salvati si perdono. Per una demo va bene; per persistenza serve uno storage Azure Files (non trattato qui).

---

## 11. Verificare i servizi

Apri nel browser:

```text
https://<auth-fqdn>/docs
https://<order-fqdn>/docs
...
```

FastAPI serve automaticamente la pagina `/docs` con la documentazione interattiva. Se la vedi, il servizio è online.

Puoi stampare tutti gli URL in una volta:

```bash
echo "AUTH   $AUTH_URL"
echo "ORDER  $ORDER_URL"
echo "INV    $INV_URL"
echo "PAY    $PAY_URL"
echo "SHIP   $SHIP_URL"
```

**Se un servizio non risponde:** guarda i log.

```bash
az containerapp logs show --name order-service --resource-group $RG --follow
```

Ricordati di sostituire il nome del servizio.

---

## 12. Collegare il frontend agli URL di Azure

Il file `frontend/script.js` oggi costruisce gli URL da `location.hostname` e dalle porte locali. Su Vercel questo non funziona, perché il dominio è diverso e le porte 8000-8004 non esistono.

Prima della modifica, cerca eventuali altri usi della variabile `H`:

```bash
grep -n "\bH\b" frontend/script.js
```

Il file `frontend/script.js` è già stato modificato così:

- `AZURE_URLS` contiene gli URL HTTPS dei servizi, con segnaposto `AUTH-FQDN`, `ORDER-FQDN`, ecc.
- Se la pagina è su un dominio `*.vercel.app`, il frontend usa `AZURE_URLS`.
- Altrove (locale, `docker compose`) usa le porte 8000-8004 come prima.

**Devi sostituire i segnaposto** con gli URL stampati al passo 11, senza `/` finale, per esempio:

```js
const AZURE_URLS = {
  auth:      "https://auth-service.xxxx.westeurope.azurecontainerapps.io",
  // ...
};
```

**Perché HTTPS:** la pagina su Vercel è servita in HTTPS. Il browser blocca le richieste HTTP da una pagina HTTPS (si chiama *mixed content*). Gli URL Azure sono già HTTPS, quindi non ci sono problemi.

Non è necessario cambiare il CORS: i servizi accettano già richieste da qualunque origine (`allow_origins=["*"]`).

---

## 13. Pubblicare il frontend su Vercel

1. Assicurati che la modifica al punto 12 sia committata e pushata su GitHub:
   ```bash
   git add frontend/script.js docs/DEPLOY.md
   git commit -m "Configura URL backend per deploy"
   git push origin main
   ```
   (Questi comandi non sono ancora stati eseguiti: li lanci tu quando sei pronto.)
2. Vai su [vercel.com](https://vercel.com) → **Add New… → Project** → importa il repository.
3. Impostazioni del progetto:
   - **Root Directory:** `frontend`. Vercel pubblicherà solo quella cartella.
   - **Framework Preset:** `Other`.
   - **Build Command:** lasciare vuoto (non c'è nessuna build: sono file statici).
   - **Output Directory:** lasciare vuoto.
4. **Deploy.** Dopo qualche secondo ottieni un URL tipo `https://tuo-progetto.vercel.app`.

**Perché così:** il frontend non ha bisogno di compilazione. Vercel serve i file così come sono, con HTTPS automatico.

---

## 14. Test end-to-end

1. Apri l'URL di Vercel.
2. Fai login dalla pagina: le chiamate vanno ad `auth-service`.
3. Crea un ordine: il frontend chiama `order-service`, che coordina inventory, payment e shipping.
4. Verifica sulla dashboard Stripe (modalità test) che l'autorizzazione del pagamento compaia.

Se qualcosa fallisce, apri la **console del browser** (F12 → Console e Network): ti dice quale URL ha restituito errore e con quale codice.

---

## 15. Problemi frequenti

| Sintomo | Causa probabile | Soluzione |
|---|---|---|
| Errore di *mixed content* nella console | Un URL nel frontend è `http://` | Usa solo `https://` in `URL_`. |
| `401 Unauthorized` su order | `JWT_SECRET` diverso tra auth e order | Ricrea i due servizi con lo stesso segreto. |
| Il container non parte, errore di pull | Credenziali ACR sbagliate o admin disabilitato | Ripeti il passo 4 e il passo 7. |
| `Provider not registered` | Passo di registrazione saltato | Esegui i comandi `az provider register` del passo 2. |
| Errori Stripe nel payment | Chiave non valida o non di test | Controlla che sia `sk_test_...` e che sia impostata come secret. |
| Il primo accesso è lento | Container a zero repliche | Imposta `--min-replicas 1` (già nei comandi). |
| Ordini spariti dopo un riavvio | Filesystem effimero | Vedi nota al passo 10. |

Per vedere i log di un servizio:

```bash
az containerapp logs show --name <nome-servizio> --resource-group $RG --follow
```

---

## 16. Pulizia (per non pagare dopo la demo)

```bash
az group delete --name $RG --yes --no-wait
```

**Cosa fa:** elimina il resource group e tutto ciò che contiene (container, registro, environment, log). È l'ultimo passo da fare quando la demo è finita.

---

## Glossario rapido

- **Container:** processo isolato che esegue un'immagine Docker.
- **Immagine:** modello immutabile del container, costruito dal `Dockerfile`.
- **Registro (registry):** dove si conservano le immagini.
- **HTTPS:** HTTP cifrato con certificato. Container Apps e Vercel lo forniscono automaticamente.
- **CORS:** regola del browser che decide se una pagina di un dominio può chiamare un altro dominio. Qui è aperto con `*`.
- **JWT:** token firmato che `auth-service` emette e `order-service` verifica.
- **TCC (Try-Confirm/Cancel):** pattern per transazioni distribuite, spiegato nel `README.md` del progetto.
