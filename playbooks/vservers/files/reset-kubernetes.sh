#!/usr/bin/env bash

LIBVIRT_FOLDER=/var/lib/libvirt/images
BASE_IMAGE="base-ubuntu-2204-full"

VOLUME_SIZE=50G

for domain in $(sudo virsh list --all | grep -E "k8s-.*-(master|node)-.*" | awk '{print $2}'); do
	echo "Trying to shutdown domain \"$domain\""
	sudo virsh shutdown $domain

	echo "Waiting for domain to be shut down"
	sleep 30

	echo "Deleting domain storage"
	sudo rm -f "${LIBVIRT_FOLDER}/${domain}-os"
	
	echo "Trying to create image"
	if echo "${domain}" | grep -qE "k8s-.*-node-.*"; then
	    VOLUME_SIZE=175G
	else
	    VOLUME_SIZE=50G
	fi
	sudo qemu-img create -f qcow2 -F qcow2 -o backing_file="${LIBVIRT_FOLDER}/${BASE_IMAGE}" "${LIBVIRT_FOLDER}/${domain}-os" "${VOLUME_SIZE}"

	echo "Starting domain \"$domain\""
	sudo virsh start $domain
done
