#!/usr/bin/env bash

NODES=(vserver.pt.soerenschneider.net nuc.pt.soerenschneider.net vserver.dd.soerenschneider.net brick.dd.soerenschneider.net rack.ez.soerenschneider.net)

for node in ${NODES[@]}; do
	ssh-keygen -R $node 
	ssh-keyscan $node >> ~/.ssh/known_hosts
done
