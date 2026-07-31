# Chunk 1 — Minimal 2-Router Topology

**Goal:** Two FRR routers connected by a single link, ping verified (0% packet loss).  
**Status:** ✅ Done — merged to master (PR #1)

---

## Prerequisites

Install Docker, Containerlab, and pull the FRR image before starting.

```bash
# Remove conflicting packages and install Docker
sudo dnf remove -y moby-engine docker-ce-cli
sudo dnf install -y docker-ce docker-ce-cli containerd.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER && newgrp docker
docker run hello-world                         # should print "Hello from Docker!"

# Install Containerlab
bash -c "$(curl -sL https://get.containerlab.dev)"
sudo usermod -aG clab_admins $USER && newgrp clab_admins
containerlab version                           # should show 0.76.1+

# Pull FRR router image
docker pull frrouting/frr:latest
```

---

## File Structure

```
phase1-simulation/
├── configs/
│   ├── r1/
│   │   ├── daemons       # which FRR daemons to run
│   │   └── frr.conf      # router config (IP assignment)
│   └── r2/
│       ├── daemons
│       └── frr.conf
└── topology/
    └── step1.clab.yml    # network blueprint
```

---

## Key Files

**`topology/step1.clab.yml`** — Defines the two nodes and the link between them.  
**`configs/r1/frr.conf`** — Assigns `10.0.0.1/24` to r1's eth1.  
**`configs/r2/frr.conf`** — Assigns `10.0.0.2/24` to r2's eth1.  
**`configs/*/daemons`** — Enables `zebra` (core); `ospfd`/`staticd` ready for Chunk 2.

---

## Deploy & Test

```bash
cd phase1-simulation/topology

# Build the lab
sudo clab deploy -t step1.clab.yml

# Test connectivity — expect 0% packet loss
docker exec clab-step1-r1 ping -c 3 10.0.0.2

# Tear down when done
sudo clab destroy -t step1.clab.yml
```

---

## Team Checkpoint

Every teammate must reproduce the ping on their own machine before Chunk 2 starts.

```bash
git clone https://github.com/ankamteja/air-gapped-mpls-copilot
cd air-gapped-mpls-copilot/phase1-simulation/topology
docker pull frrouting/frr:latest
sudo clab deploy -t step1.clab.yml
docker exec clab-step1-r1 ping -c 3 10.0.0.2
sudo clab destroy -t step1.clab.yml
```

If anyone gets packet loss, fix that machine before proceeding.

---

## Git Workflow Used

```bash
git checkout -b charan/step1-topology
git add .gitignore phase1-simulation/
git commit -m "Phase 1 Chunk 1: two FRR routers with working ping"
git push origin charan/step1-topology
# Opened PR on GitHub: base master <- compare charan/step1-topology → merged
```

> **Note:** `clab-step1/` runtime folders are in `.gitignore` — never commit them.

---

## What's Next

→ **Chunk 2 — MPLS Core:** 2 PE + 1 P router, OSPF for reachability, LDP for label distribution, verify label-switched path.
