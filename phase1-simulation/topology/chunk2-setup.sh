#!/bin/bash
# Run AFTER: sudo clab deploy -t chunk2.clab.yml
set -e

sudo modprobe mpls_router
sudo modprobe mpls_iptunnel

docker exec clab-chunk2-pe1 sysctl -w net.mpls.platform_labels=100000
docker exec clab-chunk2-pe1 sysctl -w net.mpls.conf.eth1.input=1
docker exec clab-chunk2-p1  sysctl -w net.mpls.platform_labels=100000
docker exec clab-chunk2-p1  sysctl -w net.mpls.conf.eth1.input=1
docker exec clab-chunk2-p1  sysctl -w net.mpls.conf.eth2.input=1
docker exec clab-chunk2-pe2 sysctl -w net.mpls.platform_labels=100000
docker exec clab-chunk2-pe2 sysctl -w net.mpls.conf.eth1.input=1

docker exec clab-chunk2-pe1 vtysh -c "configure terminal" -c "interface lo" -c "ip ospf area 0"
docker exec clab-chunk2-p1  vtysh -c "configure terminal" -c "interface lo" -c "ip ospf area 0"
docker exec clab-chunk2-pe2 vtysh -c "configure terminal" -c "interface lo" -c "ip ospf area 0"

echo "Waiting 25s for OSPF to converge..."
sleep 25

docker exec clab-chunk2-pe1 vtysh -c "configure terminal" -c "mpls ldp" -c "router-id 1.1.1.1" -c "address-family ipv4" -c "discovery transport-address 1.1.1.1" -c "interface eth1" -c "interface lo" -c "exit-address-family"
docker exec clab-chunk2-p1  vtysh -c "configure terminal" -c "mpls ldp" -c "router-id 2.2.2.2" -c "address-family ipv4" -c "discovery transport-address 2.2.2.2" -c "interface eth1" -c "interface eth2" -c "interface lo" -c "exit-address-family"
docker exec clab-chunk2-pe2 vtysh -c "configure terminal" -c "mpls ldp" -c "router-id 3.3.3.3" -c "address-family ipv4" -c "discovery transport-address 3.3.3.3" -c "interface eth1" -c "interface lo" -c "exit-address-family"

echo "Waiting 45s for LDP sessions..."
sleep 45
echo "--- LDP neighbors (expect OPERATIONAL) ---"
docker exec clab-chunk2-pe1 vtysh -c "show mpls ldp neighbor"
echo "--- Ping pe1 -> pe2 (expect 0% loss) ---"
docker exec clab-chunk2-pe1 ping -c 3 3.3.3.3
