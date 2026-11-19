# MyDrive — personal cloud storage for your Oracle VM

A lightweight Google-Drive-style storage server for a 1 GB RAM Oracle Always Free VM
(`VM.Standard.E2.1.Micro`), using your 150 GB block volume.

```
 Browser / phone ──► Web app ─┐
 Your projects ───► REST API ─┤                         ┌──────────────────────────┐
 Claude ──────────► MCP ──────┼─► Caddy (HTTPS) ─► MyDrive (FastAPI, ~80 MB RAM) ─► /data (150 GB volume)
 Explorer/Finder ─► WebDAV ───┘                         └──────────────────────────┘
```

**Features**
- Web app: upload files and folders (drag and drop), folders, preview (images, video, audio, PDF, text),
  search, rename, move, copy, zip download, trash (30 days), public share links with expiry, dark mode, mobile layout
- Multiple users: each family member has a private space, a **Family shared** folder everyone can use,
  optional per-user quotas, and an admin user-management page
- API keys per app: limited to one folder, optionally read-only, revocable
- **Claude connector (MCP)**: Claude can list, read, write, search, move, delete (to trash) and share files
- REST API with interactive docs at `/api/docs`
- WebDAV at `/dav/`: map it as a network drive in Windows, macOS, iOS or Android, or use rclone
- Daily database snapshot, automatic trash purge, automatic HTTPS (Let's Encrypt via Caddy)

---

## 1. Install

### a) Open ports 80 and 443 in the Oracle console (one time)
Oracle Cloud console → **Networking → Virtual cloud networks** → your VCN → **Security Lists** →
*Default Security List* → **Add Ingress Rules**:

| Source CIDR | IP Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

(The installer opens the same ports in the VM's own firewall.)

### b) Run the installer on the VM
Log in to the VM (use `opc@` instead of `ubuntu@` if it runs Oracle Linux):
```bash
ssh -i path/to/your-ssh-key ubuntu@152.70.71.35
```
Then run this one command:
```bash
curl -fsSL https://raw.githubusercontent.com/psprashantsingla/mydrive/main/get.sh | sudo bash
```

The installer:
1. adds 2 GB of swap
2. finds the 150 GB volume, **asks before formatting it** (an existing filesystem is mounted as-is),
   mounts it at `/data` and adds it to `/etc/fstab`
3. installs the app (into a Python 3.12 environment) and Caddy
4. opens ports 80 and 443 in the VM firewall
5. starts everything as services that restart on reboot
6. asks you to create your admin account

> If no volume is found and its attachment type is **iSCSI**: in the console, open the instance →
> *Attached block volumes* → ⋮ → *iSCSI commands & information*, run the **attach** commands on the VM,
> then run the install command again.

### c) Open it
**https://152-70-71-35.sslip.io**. `sslip.io` is a free DNS service that maps that name to your IP,
which lets the server get a real HTTPS certificate without you buying a domain.

---

## 2. Add family members
Sign in as admin → **Users** → *Add a family member* (username, initial password, optional quota in GB).
They sign in with their own account, see only their own files plus **Family shared**, and can change
their password under **Account**.

---

## 3. Connect Claude
In MyDrive → **Apps & Claude** → create a key (for example "Claude").

- Choose **Folder** to limit what Claude can see, e.g. `/claude` or `/projects`. `/` gives access to everything.
- Choose **Read only** if Claude should only read.

The key is shown **once**, with ready-to-copy instructions:

**claude.ai / Claude Desktop / Claude mobile:** Settings → Connectors → *Add custom connector* →
URL `https://152-70-71-35.sslip.io/mcp/mdk_YOUR_KEY`. Treat this URL like a password.

**Claude Code:**
```bash
claude mcp add --transport http mydrive https://152-70-71-35.sslip.io/mcp --header "Authorization: Bearer mdk_YOUR_KEY"
```

Claude's tools: `list_files`, `read_file`, `write_file`, `create_folder`, `move`, `copy`, `delete` (to trash),
`search`, `get_info`, `create_share_link`, `storage_usage`. Read-only keys only get the read tools.

---

## 4. Use from your projects (REST API)
Create a separate key per project, limited to that project's folder. That folder becomes the key's `/`.
Full interactive docs: `https://152-70-71-35.sslip.io/api/docs`.

```bash
KEY=mdk_...; D=https://152-70-71-35.sslip.io
curl -H "Authorization: Bearer $KEY" "$D/api/files?path="                                # list
curl -H "Authorization: Bearer $KEY" -T photo.jpg "$D/api/upload?path=photos/photo.jpg"   # upload (streams, any size)
curl -H "Authorization: Bearer $KEY" -o photo.jpg "$D/api/download?path=photos/photo.jpg" # download (supports Range)
curl -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
     -d '{"path":"notes/todo.md","content":"# hi"}' "$D/api/write"                        # write text
```

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/files?path=` | list a folder |
| GET | `/api/stat?path=` | file info |
| GET | `/api/download?path=&inline=0` | download (folder gives a zip) |
| GET | `/api/zip?paths=a&paths=b` | zip several items |
| PUT | `/api/upload?path=&conflict=rename\|overwrite\|error` | upload (raw request body) |
| POST | `/api/write` `{path, content}` | write a text file |
| POST | `/api/mkdir` `{path}` | create a folder |
| POST | `/api/move` / `/api/copy` `{src, dst, overwrite}` | move, rename or copy |
| POST | `/api/delete` `{paths: [...]}` | move to trash |
| GET | `/api/search?q=&path=` | search by name |
| POST | `/api/shares` `{path, expires_days}` | create a public link |
| GET | `/api/me` | usage and quota |

## 5. Network drive (WebDAV)
Address `https://152-70-71-35.sslip.io/dav/`. Sign in with your username and password, or with
your username and an API key to restrict access to that key's folder.

- **Windows:** File Explorer → This PC → *Map network drive*
- **macOS:** Finder → Go → *Connect to Server* (⌘K)
- **iPhone:** Files → ⋯ → *Connect to Server*
- **rclone:**
  ```bash
  rclone config create mydrive webdav url=https://152-70-71-35.sslip.io/dav vendor=other user=YOU pass=$(rclone obscure mdk_KEY)
  ```

---

## 6. Maintenance

| Task | Command (on the VM) |
|---|---|
| Update MyDrive | run the same one-line install command again (your data and settings are kept) |
| List users | `sudo mydrive-admin list-users` |
| Reset a password | `sudo mydrive-admin reset-password NAME` |
| Create a user | `sudo mydrive-admin create-user NAME [--admin] [--quota-gb 20]` |
| Logs | `journalctl -u mydrive -f` and `journalctl -u caddy -f` |
| Restart | `sudo systemctl restart mydrive caddy` |
| Disk usage | `df -h /data` |

**Where things are stored**
- `/data/files/users/<name>/`: each user's files (plain files you can also reach over SSH)
- `/data/files/shared/`: Family shared
- `/data/trash/`: trash
- `/data/mydrive.db`: users, keys and links. A copy is saved daily to `/data/backups/` (14 days kept).

**Backups:** a single volume is not a backup. Options:
- In the Oracle console, enable a backup policy on the block volume (Always Free includes 5 volume backups).
- Copy important folders elsewhere with rclone, for example `rclone sync mydrive:Photos gdrive:Photos-backup`.

## 7. Troubleshooting
- **Site doesn't load or the HTTPS certificate fails:** check the Security List rules (step 1a),
  then run `journalctl -u caddy -n 50`.
- **"Not enough storage space":** the user's quota is reached, or the volume has less than 512 MB free.
- **Forgot the admin password:** run `sudo mydrive-admin reset-password NAME`.
- **The 150 GB disk is missing after a reboot:** for iSCSI attachments, re-run the attach commands
  (or switch the attachment to paravirtualized). MyDrive doesn't start while `/data` isn't mounted,
  so it never writes to the small boot disk by mistake.

## Security notes
- All traffic is HTTPS. Passwords are hashed with scrypt. Repeated failed logins are rate-limited.
- API keys are stored hashed and can be limited to one folder and read-only.
- Uploaded HTML and SVG files are never rendered as web pages on your domain (protects against XSS).
- Share links are random, can expire, and can be revoked under **Share links**.
- Deletes from any app or from Claude go to the trash first.
