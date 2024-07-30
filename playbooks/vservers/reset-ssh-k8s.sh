#!/usr/bin/env bash

NODES=(k8s-prd-master-1.dd.soerenschneider.net k8s-prd-master-2.ez.soerenschneider.net k8s-prd-master-3.pt.soerenschneider.net k8s-prd-node-dd-1.dd.soerenschneider.net k8s-prd-node-ez-1.ez.soerenschneider.net k8s-prd-node-dd-2.dd.soerenschneider.net k8s-prd-node-pt-1.pt.soerenschneider.net k8s-prd-node-pt-2.pt.soerenschneider.net)

for node in ${NODES[@]}; do
	ssh-keygen -R $node
	ssh-keyscan $node >> ~/.ssh/known_hosts
done
