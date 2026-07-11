# Running Magpie on Windows

A complete, click-by-click guide — **no coding needed**. New here? The main **[README](../README.md)** explains what Magpie is, how it thinks and the safety rails; this page is just how to install, run, update and remove it on Windows.

You don't need to be technical or write any code. Magpie runs inside **Docker
Desktop** — a free app that does the hard part for you. Here's the whole thing,
step by step.

> **🖥️ Magpie needs to stay running.** It only makes decisions when the machine
> is switched on, Docker Desktop is running, and the scheduled tasks can fire. So
> install it on a computer that's on all (or most) of the time — an always-on
> desktop, a mini-PC or a home server is ideal; a laptop that sleeps or gets shut
> down will simply skip decisions until it's back on (no harm, but it can't trade
> while it's off). On whatever machine you pick, set Windows to **never sleep**
> (Settings → System → Power → Screen and sleep) and set **Docker Desktop to start
> when you sign in** (Docker Desktop → Settings → General).

### 1. Install Docker Desktop
- Go to **[docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)** and click **Download for Windows**.
- Run the file you downloaded, click through with the default options, and **restart your PC** if it asks you to.
- Open **Docker Desktop** from the Start menu. The first launch takes a minute — wait until the whale icon near your clock stops animating and the app says **"Engine running"**. Leave it open.

### 2. Download Magpie
- Go to **[github.com/colfin22/magpie](https://github.com/colfin22/magpie)**.
- Click the green **`<> Code`** button, then **Download ZIP**.
- Find the ZIP in your **Downloads**, right-click it → **Extract All…** → **Extract**. You'll get a folder named `magpie-master`. Move it somewhere easy to find, like your **Documents**.

### 3. Enter your keys
- Open the `magpie-master` folder and find the file **`.env.example`**.
- **Copy it** (click it, Ctrl+C, then Ctrl+V) and **rename the copy to exactly `.env`** — delete the `.example` part. *(If Windows won't let a name start with a dot, type `.env.` with a dot on the end and Windows removes it for you.)*
- **Right-click `.env` → Open with → Notepad.** Fill in:
  - your **Kraken** API key and secret (create it with *query + trade* only — **never** withdrawal),
  - one **LLM** key — a free **[Gemini key](https://aistudio.google.com/apikey)** is the easiest start,
  - your **`TIMEZONE`** (e.g. `America/New_York`),
  - leave **`TRADING_ENABLED=false`** so it starts in safe **paper mode**.
- **Save** and close Notepad.

### 4. Start it
- Open the `magpie-master` folder. Click the **address bar** at the top (where the folder path is), type **`powershell`**, and press **Enter** — a blue window opens, already pointed at the folder.
- Type this line and press **Enter**:
  ```
  docker compose up -d --build
  ```
- The first run downloads and builds everything — give it a few minutes. It's finished when the blue window shows a fresh prompt again.

### 5. Open the dashboard
- In your web browser, go to **[http://localhost:8000](http://localhost:8000)**. That's Magpie.
- It's in **paper mode** (pretend money), so you can watch it make decisions with zero risk before committing real money.

### 6. Keep it deciding automatically
Magpie only makes decisions when it's "poked" a few times a day. On Windows you set that up once with **Task Scheduler** (the built-in Windows scheduler):
- Open the Start menu, type **PowerShell**, **right-click → Run as administrator**, and click **Yes**.
- Paste these four lines (all at once is fine) and press **Enter**:
  ```
  schtasks /create /tn "Magpie cycle"     /sc daily /st 00:00 /ri 360 /du 24:00 /tr "curl.exe -X POST http://localhost:8000/api/cycle"
  schtasks /create /tn "Magpie digest"    /sc daily /st 18:05 /tr "curl.exe -X POST http://localhost:8000/api/digest"
  schtasks /create /tn "Magpie reconcile" /sc daily /st 05:45 /tr "curl.exe -X POST http://localhost:8000/api/reconcile"
  schtasks /create /tn "Magpie review"    /sc monthly /d 1 /st 05:30 /tr "curl.exe -X POST http://localhost:8000/api/review"
  ```
- That pokes the bot every 6 hours (midnight, 6am, noon, 6pm) plus a daily summary and a monthly review. Because you set your `TIMEZONE` in step 3, these line up with your own clock.

### Going live with real money
When you're happy watching the paper-mode diary, open **`.env`** in Notepad, change `TRADING_ENABLED=false` to **`TRADING_ENABLED=true`**, save, then in the blue PowerShell window run:
```
docker compose up -d --force-recreate
```
From then on it trades the real balance in your (dedicated, empty) Kraken account. Remember the [empty-account warning](../README.md#run) above.

### If something isn't working
- **Nothing loads / commands fail?** Make sure **Docker Desktop is open and shows "Engine running"** — Magpie can't run without it. To avoid this, set it to start automatically: Docker Desktop → **Settings → General → *Start Docker Desktop when you sign in***.
- **`http://localhost:8000` won't open?** Wait a minute after step 4 and refresh — the first build takes a moment to finish.
- **Do I need Python, Linux or WSL?** No. Docker Desktop sets up everything it needs behind the scenes.

## Updating Magpie

Don't worry — your settings and history are kept safe for you. Your keys live in a
file called **`.env`**, and everything Magpie has recorded (your trades, settings
and 2FA) lives in a folder called **`data`**. Neither of those is inside the
download, so updating can't overwrite them — you're only swapping in newer program
files.

1. **See what's new.** Visit the **[latest release](https://github.com/colfin22/magpie/releases)** page to check what changed.
2. **Download the new version.** Go to **[github.com/colfin22/magpie](https://github.com/colfin22/magpie)**, click the green **`<> Code`** button, then **Download ZIP**. Find the ZIP in your **Downloads**, right-click it → **Extract All… → Extract**.
3. **Select and copy the new files.** Open the newly extracted `magpie-master` folder. Press **Ctrl+A** to highlight everything inside, then **Ctrl+C** to copy it.
4. **Paste them into your existing Magpie folder.** Open the Magpie folder you already have and press **Ctrl+V**. Windows will say some files already exist — choose **"Replace the files in the destination."** *(This only replaces the program files. Your `.env` and `data` folder aren't in the download, so they're left exactly as they were.)*
5. **Rebuild it.** In your Magpie folder, click the **address bar** at the very top (where the folder name shows), type **`powershell`**, and press **Enter** — a blue window opens, already pointing at the folder. Type this line and press **Enter**:
   ```
   docker compose up -d --build
   ```
   Wait until the blue window shows a fresh prompt again.
6. **Open the dashboard.** Go to **[http://localhost:8000](http://localhost:8000)** and press **Ctrl+Shift+R** — that makes your browser load the new version instead of a saved copy.

Done. Your holdings, diary, settings and 2FA all carry over, and the scheduled
tasks you set up earlier keep working — nothing to redo.

> **⚠️ Never delete the `data` folder.** It *is* your database — your history,
> settings and your 2FA. Deleting it wipes Magpie back to a blank install.

*(If you set Magpie up with `git clone` rather than the ZIP, updating is just
`git pull` in the folder, then `docker compose up -d --build`.)*

## Uninstalling or starting over

Everything below is typed into a **blue PowerShell window opened in your Magpie
folder**. To open one: open your Magpie folder, click the **address bar** at the
top, type **`powershell`**, and press **Enter**.

### Just pause it for a while (keeps everything)
Type this and press **Enter**:
```
docker compose stop
```
When you want it back, type `docker compose start` and press **Enter**.

### Remove Magpie completely
1. In the blue PowerShell window, type this and press **Enter**. It stops and
   deletes the app and frees up the disk space:
   ```
   docker compose down --rmi all
   ```
2. **Delete the Magpie folder.** Close the PowerShell window, then right-click your
   Magpie folder and choose **Delete**. *(This removes the program, your `.env`
   keys and the `data` database.)*
3. **Remove the scheduled reminders.** Click **Start**, type **PowerShell**,
   right-click it and choose **Run as administrator**, then **Yes**. Paste these
   four lines and press **Enter**:
   ```
   schtasks /delete /tn "Magpie cycle" /f
   schtasks /delete /tn "Magpie digest" /f
   schtasks /delete /tn "Magpie reconcile" /f
   schtasks /delete /tn "Magpie review" /f
   ```
4. **(Optional) Remove Docker Desktop** if you don't use it for anything else:
   Windows **Settings → Apps → Installed apps → Docker Desktop → Uninstall**.

> **💰 Your money is safe and separate.** Removing Magpie does **nothing** to your
> Kraken account — your balance stays on Kraken exactly where it is. To get your
> money, sell or withdraw on **Kraken** directly. And once Magpie is gone, go to
> your Kraken account settings and **delete its API key** so nothing can use it
> again.

### Start over fresh, but keep using Magpie
This wipes the history and gives you a clean slate while keeping the app installed:
1. In the blue PowerShell window, type `docker compose down` and press **Enter** (this stops it).
2. Open your Magpie folder and delete just the **`data`** folder (right-click → **Delete**).
3. Back in the PowerShell window, type `docker compose up -d --build` and press **Enter**.

It comes back brand new — paper mode, an empty diary, and you choose your base
currency again.
