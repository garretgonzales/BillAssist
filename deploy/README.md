# Deploying to a Compute Engine VM behind IAP

Nothing here can be run from this session - it needs `gcloud` authenticated
to your actual Google account, which only you can do. This is the full
path from zero to "opening a tunnel and using the app."

## What this gets you

- A Compute Engine VM with **no public IP at all**. The only way to reach
  it is an IAP tunnel, which Google only opens for identities you've
  explicitly granted - in practice, just you.
- No load balancer, no managed cert, no extra always-on cost beyond the
  VM itself (an e2-small is roughly $13-15/mo; e2-micro can be $0 under
  the free tier in `us-west1`/`us-central1`/`us-east1`).
- The tradeoff: it's `http://localhost:PORT` in your own browser after
  running one `gcloud` command, not a link you can just click from your
  phone. See the session's earlier note if you want a real HTTPS URL
  instead - that needs a Load Balancer in front, which is a bigger step
  up in cost and moving parts.

## 1. One-time GCP setup

Edit the variables at the top of `gcp-setup.sh` (project ID, zone, your
Google account), read through it, then run it:

```
bash deploy/gcp-setup.sh
```

This creates the VM and firewall rule, and grants your account the two
IAM roles needed to tunnel and SSH in.

## 2. Get the code onto the VM

From your local machine (not the VM):

```
gcloud compute scp --recurse --tunnel-through-iap \
  --zone=us-central1-a \
  --exclude='venv,bills.db,token.json,__pycache__,flask_secret_key.txt' \
  /path/to/BillTracker \
  bill-tracker:~/BillTracker
```

(`gcloud compute scp` doesn't actually support `--exclude` - simplest is
to `rsync` over the same tunnel instead, or just delete `venv/` locally
before copying since it's platform-specific and pip-installable on the
VM anyway. `bills.db` and `token.json` are deliberately left out here -
see "Bringing your data over" below for whether you want to copy them.)

## 3. Set up the app on the VM

SSH in:

```
gcloud compute ssh bill-tracker --zone=us-central1-a --tunnel-through-iap
```

Then, on the VM:

```
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
cd ~/BillTracker
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

You still need Gmail's `credentials.json` on the VM if you want scanning
or reminders there too - upload it the same way as the code (`gcloud
compute scp`), or paste its contents into a file over SSH. Don't commit
it or leave a copy lying around outside the VM's `~/BillTracker/`.

## 4. Install it as a service

```
sudo cp ~/BillTracker/deploy/billtracker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now billtracker
sudo systemctl status billtracker   # should say active (running)
```

It's now running gunicorn on port 8080 internally, restarting
automatically if it ever crashes or the VM reboots.

## 5. Open the tunnel and use it

Every time you want to use it:

```
gcloud compute start-iap-tunnel bill-tracker 8080 \
  --local-host-port=localhost:5432 \
  --zone=us-central1-a
```

Leave that running, then open http://127.0.0.1:5432 in your browser.
Worth a shell alias, e.g. in `~/.bashrc`:

```
alias billtracker-tunnel='gcloud compute start-iap-tunnel bill-tracker 8080 --local-host-port=localhost:5432 --zone=us-central1-a'
```

## Gmail OAuth on the VM

The redirect URI is always `http://localhost:5432/oauth2callback` by
design - that's what your browser's address bar shows whether the app is
truly local or reached through the tunnel, since the tunnel forwards a
local port. Your existing "Desktop app" `credentials.json` should keep
working as-is, since Google allows flexible loopback redirect URIs for
that client type.

If Google instead rejects it with `redirect_uri_mismatch`: create a new
OAuth client of type **Web application** in Cloud Console instead, add
`http://localhost:5432/oauth2callback` to its Authorized redirect URIs,
download that JSON, and use it as `credentials.json` on the VM (delete
`token.json` too, so it does a fresh consent under the new client).

## Bringing your data over

Nothing copies your real `bills.db` or Gmail `token.json` automatically -
that's deliberate, since it's your financial data and the choice to
duplicate it onto a cloud VM should be explicit. If you want the VM to
start with what you've already got locally:

```
gcloud compute scp --tunnel-through-iap --zone=us-central1-a \
  /path/to/BillTracker/bills.db \
  bill-tracker:~/BillTracker/bills.db
```

Otherwise it starts empty and you re-add bills there directly.

## Updating the app later

```
gcloud compute scp --recurse --tunnel-through-iap --zone=us-central1-a \
  /path/to/BillTracker/*.py /path/to/BillTracker/templates /path/to/BillTracker/static \
  bill-tracker:~/BillTracker/
gcloud compute ssh bill-tracker --zone=us-central1-a --tunnel-through-iap \
  --command="sudo systemctl restart billtracker"
```

## Daily reminder timer

`daily_reminders.py` loops over every user and sends each one their own
reminder digest (their own bills, their own `reminder_leads` settings,
their own Gmail) if they're due one and haven't already gotten one
today - see `_maybe_send_daily_reminder` in `app.py`. It needs to run
once a day independently of whether anyone has the app open in a
browser, on **both** the VM (so it fires even if your local machine is
off) and locally (so it still works before/without the VM being set
up). Each is installed separately - one user, one household, but two
places the timer needs to exist.

### On the VM (system-level timer, needs root)

```
sudo cp ~/BillTracker/deploy/billtracker-reminders.service /etc/systemd/system/
sudo cp ~/BillTracker/deploy/billtracker-reminders.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now billtracker-reminders.timer
sudo systemctl list-timers billtracker-reminders.timer   # confirm it's scheduled
```

Run it once by hand to confirm it actually works before trusting the
timer: `sudo systemctl start billtracker-reminders.service`, then
`sudo journalctl -u billtracker-reminders.service -n 50`.

### Locally (user-level timer, no root, no sudo)

```
mkdir -p ~/.config/systemd/user
cp deploy/billtracker-reminders-local.service ~/.config/systemd/user/
cp deploy/billtracker-reminders-local.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now billtracker-reminders-local.timer
```

User-level systemd units only run while you're logged in, by default -
run `loginctl enable-linger $(whoami)` once so it still fires even when
you're logged out (e.g. overnight), since 8am is likely before you've
logged back in.

Test it by hand the same way:
`systemctl --user start billtracker-reminders-local.service`, then
`journalctl --user -u billtracker-reminders-local.service -n 50`.

### Changing the time

Both timers default to 8:00am (`OnCalendar=*-*-* 08:00:00` in the
`.timer` file) - edit that line, then re-run the `cp`/`daemon-reload`
steps above for whichever one you changed.
