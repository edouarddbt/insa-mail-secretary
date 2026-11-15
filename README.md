# Secrétaire IA — Messagerie INSA Rouen (ITI4)

Assistant personnel **24/7** qui surveille ta boîte mail INSA (`partage.insa-rouen.fr` en IMAP SSL/TLS), trie tes e-mails avec **Gemini** (API gratuite) et t'envoie uniquement ce qui compte sur **Telegram**.

---

## Fonctionnalités

- **Alertes en direct sur Telegram** : uniquement pour les mails urgents, importants ou qui te concernent personnellement (profs, scolarité, stages, alternance, projets, convocations...).
- **Boutons d'action directe sous chaque alerte** :
  - **📖 Lire** : affiche le contenu complet du mail (et des pièces jointes PDF).
  - **✅ Lu** : marque le mail comme lu dans ta boîte INSA.
  - **🗑️ Corbeille** : déplace le mail vers la corbeille.
  - **✍️ Répondre** : génère un brouillon de réponse prêt à copier-coller (aucun envoi automatique sans ton accord).
  - **🔕 Silencieux** : mémorise l'expéditeur en base pour ne plus te déranger en direct (sans appel IA).
  - **📅 Google Agenda** : bouton prérempli en 1 clic si une date/échéance est détectée.
- **Lecture des pièces jointes PDF** : le texte des PDF joints (convocations, règlements, plannings) est extrait automatiquement pour le tri et tes questions.
- **Silence la journée & récap du soir** : les newsletters et mails secondaires restent dans ta boîte de réception (sans notification en direct) et sont résumés dans un **récapitulatif groupé le soir** (par défaut à `20h00`).
- **Lecture non intrusive (`BODY.PEEK[]`)** : tes mails restent **non lus** sur Partage/Zimbra tant que tu ne cliques pas sur **✅ Lu**.
- **Bot conversationnel** : pose des questions en langage naturel sur tes mails (*« Est-ce que j'ai reçu un mail de la scolarité ? »*, *« Quels mails attendent une réponse ? »*).

---

## Prérequis

- **Python 3.10+** installé sur ton PC (ou sur un serveur Linux).
- Une adresse mail **INSA Rouen** (`prenom.nom@insa-rouen.fr`) et son mot de passe.
- Un compte **Google** (pour la clé API Gemini gratuite).
- Un compte **Telegram** (sur téléphone et/ou PC).

---

## Étape 1 : Obtenir ta clé API Gemini (100 % gratuit)

1. Rends-toi sur [Google AI Studio](https://aistudio.google.com/apikey) et connecte-toi avec ton compte Google.
2. Clique sur **Create API key** (Crée un projet si demandé).
3. Copie la clé générée : ce sera ton `GEMINI_API_KEY`.

> **Note sur le gratuit** : le palier gratuit de Google AI Studio est largement suffisant pour trier quelques dizaines de mails par jour. En cas de pic de charge sur un modèle, le projet bascule automatiquement sur les modèles de secours (`GEMINI_FALLBACK_MODELS`).

---

## Étape 2 : Créer ton Bot Telegram & récupérer ton Chat ID

### A. Créer le bot et obtenir le token
1. Ouvre Telegram et cherche **`@BotFather`** (vérifie le badge bleu officiel).
2. Envoie la commande `/newbot`.
3. Choisis un **nom d'affichage** (ex. `Secrétaire INSA`).
4. Choisis un **identifiant unique** finissant obligatoirement par `bot` (ex. `mon_secretaire_insa_bot`).
5. `@BotFather` te renvoie un **HTTP API token** (format `123456789:ABCdef...`) : c'est ton `TELEGRAM_BOT_TOKEN`.
6. **TRÈS IMPORTANT** : clique sur le lien vers ton nouveau bot donné par BotFather et appuie sur **Démarrer** (`/start`). *Un bot Telegram ne peut pas t'envoyer de message tant que tu ne lui as pas parlé en premier.*

### B. Récupérer ton Chat ID numérique
1. Cherche **`@userinfobot`** sur Telegram.
2. Envoie `/start`.
3. Copie la valeur de **`Id`** (un nombre, ex. `987654321`) : c'est ton `TELEGRAM_CHAT_ID`.

---

## Étape 3 : Installer et configurer le projet en local

### 1. Cloner le dépôt et installer les dépendances

**Sous Windows (PowerShell) :**
```powershell
git clone https://github.com/edouarddbt/insa-mail-secretary.git
cd insa-mail-secretary
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

**Sous macOS / Linux :**
```bash
git clone https://github.com/edouarddbt/insa-mail-secretary.git
cd insa-mail-secretary
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Remplir le fichier `.env`

Ouvre le fichier `.env` avec un éditeur de texte et renseigne tes propres informations :

| Variable | Description | Exemple |
| :--- | :--- | :--- |
| `IMAP_HOST` | Serveur IMAP de l'INSA | `partage.insa-rouen.fr` |
| `IMAP_PORT` | Port IMAP SSL | `993` |
| `IMAP_USER` | Ton adresse mail INSA | `prenom.nom@insa-rouen.fr` |
| `IMAP_PASSWORD` | Ton mot de passe INSA | `ton_mot_de_passe` |
| `GEMINI_API_KEY` | Ta clé Google AI Studio | `AIzaSy...` |
| `TELEGRAM_BOT_TOKEN` | Le token de `@BotFather` | `123456789:ABC...` |
| `TELEGRAM_CHAT_ID` | Ton ID de `@userinfobot` | `987654321` |

#### Options de personnalisation (facultatif)
- `USER_CONTEXT` : décris ton profil (département, année, mots-clés importants comme alternance, stage, options) pour guider le tri de Gemini. Par défaut, c'est déjà calibré pour un étudiant en **ITI4**.
- `VIP_SENDERS` : adresses ou domaines toujours notifiés en direct (ex. `scolarite@insa-rouen.fr`).
- `MUTED_SENDERS` : adresses ou domaines toujours mis dans le récap du soir sans appel IA (ex. `bde@insa-rouen.fr,newsletter@`).
- `DIGEST_TIME` : heure du récapitulatif du soir (défaut `20:00`).

> **Sécurité** : ne partage **jamais** ton fichier `.env` et ne le commite jamais sur GitHub (il est déjà bloqué par `.gitignore`).

---

## Étape 4 : Tester et lancer

Avant de lancer le bot en continu, vérifie chaque connexion séparément :

```bash
python -m src.main --test-imap
python -m src.main --test-telegram
python -m src.main --test-gemini
```

Si les 3 tests réussissent (tu dois recevoir un message de test sur Telegram), lance le secrétaire :

```bash
python -m src.main
```

*(Tant que le terminal reste ouvert, ton secrétaire tourne. Pour qu'il tourne 24/7 même PC éteint, passe à l'étape 6.)*

---

## Étape 5 : Utiliser le bot sur Telegram

Une fois lancé, tu peux directement discuter avec ton bot sur Telegram :

- **Questions libres sur toute ta boîte** :
  - *« Regarde dans ma corbeille si j'ai reçu un mail de la scolarité »*
  - *« À quels mails importants n'ai-je pas encore répondu ? »*
  - *« Quel est le lien ou la salle pour le cours d'anglais ? »*
- **Restaurer / déplacer des mails** :
  - *« Remets les mails importants de ma corbeille dans ma boîte de réception »* → le bot te demande confirmation avec **✅ Confirmer** / **❌ Annuler** avant toute action.
- **Commandes rapides** :
  - `/status` : état du bot et nombre de mails analysés / en attente de récap.
  - `/recap` : déclencher immédiatement le récapitulatif du soir.
  - `/aide` : afficher les exemples d'utilisation.

---

## Étape 6 : Déployer 24/7 gratuitement sur Google Cloud (`e2-micro` Always Free)

Si tu veux que ton secrétaire tourne jour et nuit sans laisser ton PC allumé, tu peux utiliser l'offre **Always Free** de Google Cloud (0 €/mois si tu respectes exactement ces réglages).

### 1. Créer la VM gratuite sur Google Cloud
Dans **Google Cloud Console → Compute Engine → Instances de VM → Créer une instance** :
1. **Région** : choisis **impérativement** une des 3 régions US éligibles au *Always Free* :
   - `us-west1` (Oregon), `us-central1` (Iowa) ou `us-east1` (Caroline du Sud).
2. **Type de machine** : série **E2** → **`e2-micro`** (2 vCPU partagés, 1 Go de RAM).
3. **Disque de démarrage (TRÈS IMPORTANT)** : clique sur **Modifier**, choisis **Ubuntu 24.04 LTS** (ou Debian 12), passe du disque *Balanced* (payant par défaut) à **Disque persistant standard** (`Standard persistent disk`), taille **≤ 30 Go** (ex. `20 Go`).
4. **Réseau** : laisse l'IP externe en mode **éphémère** (ne réserve pas d'IP statique).

### 2. Installer le projet sur la VM
Connecte-toi à la VM via le bouton **SSH** dans la console Google Cloud, puis exécute :

```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip git
sudo mkdir -p /opt/insa-mail-secretary
sudo chown "$USER":"$USER" /opt/insa-mail-secretary
git clone https://github.com/edouarddbt/insa-mail-secretary.git /opt/insa-mail-secretary
cd /opt/insa-mail-secretary

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env   # Remplis tes clés et identifiants, puis Ctrl+O, Entrée, Ctrl+X
chmod 600 .env
```

Teste la configuration sur la VM :
```bash
.venv/bin/python -m src.main --test-imap
.venv/bin/python -m src.main --test-telegram
.venv/bin/python -m src.main --test-gemini
```

### 3. Activer le service 24/7 avec `systemd`
Adapte `User=` dans `deploy/insa-secretary.service` avec ton nom d'utilisateur Linux (`whoami`), par exemple avec `sed` :

```bash
sed -i "s/^User=.*/User=$(whoami)/" deploy/insa-secretary.service
sudo cp deploy/insa-secretary.service /etc/systemd/system/insa-secretary.service
sudo systemctl daemon-reload
sudo systemctl enable --now insa-secretary
```

### Commandes utiles sur la VM
- **Voir les logs en direct** : `sudo journalctl -u insa-secretary -f`
- **Vérifier le statut** : `sudo systemctl status insa-secretary`
- **Redémarrer après modification du `.env`** : `sudo systemctl restart insa-secretary`
- **Forcer un récap immédiat** : `/opt/insa-mail-secretary/.venv/bin/python -m src.main --run-digest-now`

---

## Dépannage rapide

- **Erreur de connexion IMAP** : vérifie `IMAP_USER` (adresse complète `@insa-rouen.fr`) et `IMAP_PASSWORD`. Assure-toi que `IMAP_HOST=partage.insa-rouen.fr` et `IMAP_PORT=993`.
- **Le test Telegram échoue (`Chat not found` / `Forbidden`)** : vérifie que tu as bien cliqué sur **Démarrer** (`/start`) dans la conversation avec ton propre bot Telegram, et que `TELEGRAM_CHAT_ID` correspond bien à ton ID numérique (`@userinfobot`).
- **Aucun mail reçu au lancement** : c'est normal ! Par défaut (`INITIAL_LOOKBACK_HOURS=0`), le bot ne traite que les nouveaux mails reçus *après* son démarrage pour éviter de te spammer avec ton historique.
- **Erreur quota Gemini (429)** : si le modèle principal est saturé temporairement, le bot bascule automatiquement sur `GEMINI_FALLBACK_MODELS`.

---

## Auteurs

- **[@marwangrch](https://github.com/marwangrch)** — Idée originale
- **[@edouarddbt](https://github.com/edouarddbt)** — Développement

