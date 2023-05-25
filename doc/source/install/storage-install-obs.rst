.. _storage-obs:

Install and configure the storage nodes for openSUSE and SUSE Linux Enterprise
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This section describes how to install and configure storage nodes
that operate the account, container, and object services. For
simplicity, this configuration references two storage nodes, each
containing two empty local block storage devices. The instructions
use ``/dev/sdb`` and ``/dev/sdc``, but you can substitute different
values for your particular nodes.

Although Object Storage supports any file system with
extended attributes (xattr), testing and benchmarking
indicate the best performance and reliability on XFS. For
more information on horizontally scaling your environment, see the
`Deployment Guide <https://docs.openstack.org/swift/latest/deployment_guide.html>`_.

This section applies to openSUSE Leap 42.2 and SUSE Linux Enterprise Server
12 SP2.

Prerequisites
-------------

Before you install and configure the Object Storage service on the
storage nodes, you must prepare the storage devices.

.. note::

   Perform these steps on each storage node.

#. Install the supporting utility packages:

   .. code-block:: console

      # zypper install xfsprogs rsync

#. Format the ``/dev/sdb`` and ``/dev/sdc`` devices as XFS:

   .. code-block:: console

      # mkfs.xfs /dev/sdb
      # mkfs.xfs /dev/sdc

#. Create the mount point directory structure:

   .. code-block:: console

      # mkdir -p /srv/node/sdb
      # mkdir -p /srv/node/sdc

#. Find the UUID of the new partitions:

   .. code-block:: console

      # blkid

#. Edit the ``/etc/fstab`` file and add the following to it:

   .. code-block:: none

      UUID="<UUID-from-output-above>" /srv/node/sdb xfs noatime 0 2
      UUID="<UUID-from-output-above>" /srv/node/sdc xfs noatime 0 2

#. Mount the devices:

   .. code-block:: console

      # mount /srv/node/sdb
      # mount /srv/node/sdc

#. Create or edit the ``/etc/rsyncd.conf`` file to contain the following:

   .. code-block:: none

      uid = swift
      gid = swift
      log file = /var/log/rsyncd.log
      pid file = /var/run/rsyncd.pid
      address = MANAGEMENT_INTERFACE_IP_ADDRESS

      [account]
      max connections = 2
      path = /srv/node/
      read only = False
      lock file = /var/lock/account.lock

      [container]
      max connections = 2
      path = /srv/node/
      read only = False
      lock file = /var/lock/container.lock

      [object]
      max connections = 2
      path = /srv/node/
      read only = False
      lock file = /var/lock/object.lock

   Replace ``MANAGEMENT_INTERFACE_IP_ADDRESS`` with the IP address of the
   management network on the storage node.

   .. note::

      The ``rsync`` service requires no authentication, so consider running
      it on a private network in production environments.

7. Start the ``rsyncd`` service and configure it to start when the
   system boots:

   .. code-block:: console

      # systemctl enable rsyncd.service
      # systemctl start rsyncd.service

Install and configure components
--------------------------------

.. note::

   Default configuration files vary by distribution. You might need
   to add these sections and options rather than modifying existing
   sections and options. Also, an ellipsis (``...``) in the configuration
   snippets indicates potential default configuration options that you
   should retain.

.. note::

   Perform these steps on each storage node.

#. Install the packages:

   .. code-block:: console

      # zypper install openstack-swift-account \
        openstack-swift-container openstack-swift-object python-xml

2. .. include:: storage-include1.txt
3. .. include:: storage-include2.txt
4. .. include:: storage-include3.txt
5. Ensure proper ownership of the mount point directory structure:

   .. code-block:: console

      # chown -R swift:swift /srv/node
