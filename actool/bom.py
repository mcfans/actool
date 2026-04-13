"""
BOM (Bill of Materials) file writer.

BOM is Apple's container format used for .car files, .pkg installers, etc.
All BOM structures use big-endian byte order.
"""

import struct


class BOMWriter:
    """Writes BOM format files."""

    def __init__(self):
        self._blocks: list[bytes] = [b""]  # Block 0 is always empty
        self._named_blocks: dict[str, int] = {}
        self._trees: dict[str, int] = {}

    def add_block(self, data: bytes) -> int:
        """Add a block and return its index."""
        idx = len(self._blocks)
        self._blocks.append(data)
        return idx

    def add_named_block(self, name: str, data: bytes) -> int:
        """Add a named block (variable)."""
        idx = self.add_block(data)
        self._named_blocks[name] = idx
        return idx

    def add_tree(self, name: str, entries: list[tuple[bytes, bytes]],
                 block_size: int = 4096) -> int:
        """Add a BOM tree with the given key-value entries.

        entries: list of (key_bytes, value_bytes) pairs.
        Returns the block index of the tree header.
        """
        # Build leaf nodes. Each leaf node can hold up to block_size worth of entries.
        # Entry format in node: value_block_idx(4) + key_block_idx(4) = 8 bytes per entry
        # Node header: isLeaf(2) + count(2) + forward(4) + backward(4) = 12 bytes
        max_entries_per_node = (block_size - 12) // 8

        if len(entries) == 0:
            # Empty tree - single empty leaf node
            node_data = struct.pack(">HHiI", 1, 0, 0, 0)
            node_idx = self.add_block(node_data)
            tree_header = struct.pack(">4sIIIIBII", b"tree", 1, node_idx,
                                      block_size, 0, 0, 0, 0)
            tree_idx = self.add_block(tree_header)
            self._named_blocks[name] = tree_idx
            return tree_idx

        # Create leaf nodes
        leaf_nodes = []
        for start in range(0, len(entries), max_entries_per_node):
            batch = entries[start:start + max_entries_per_node]
            leaf_nodes.append(batch)

        if len(leaf_nodes) == 1:
            # Single leaf node - simple case
            node_data = self._build_leaf_node(leaf_nodes[0], 0, 0, block_size)
            node_idx = self.add_block(node_data)
            tree_header = struct.pack(">4sIIIIBII", b"tree", 1, node_idx,
                                      block_size, len(entries), 0, 0, 0)
            tree_idx = self.add_block(tree_header)
            self._named_blocks[name] = tree_idx
            return tree_idx

        # Multiple leaf nodes - need internal node(s)
        # Two-pass: reserve block indices first, then build with correct links
        leaf_indices = []
        for batch in leaf_nodes:
            # Reserve a block index with placeholder data
            leaf_indices.append(self.add_block(b"\x00"))

        # Now build each leaf with correct forward/backward links
        for i, batch in enumerate(leaf_nodes):
            fwd = leaf_indices[i + 1] if i + 1 < len(leaf_indices) else 0
            bwd = leaf_indices[i - 1] if i > 0 else 0
            node_data = self._build_leaf_node(batch, fwd, bwd, block_size)
            # Replace the placeholder block
            self._blocks[leaf_indices[i]] = node_data

        # Build internal node
        # Internal node format: isLeaf(2) + count(2) + forward(4) + backward(4)
        # Then: child0(4), [key_block(4) + child(4)] * count
        internal = struct.pack(">HHII", 0, len(leaf_indices) - 1, 0, 0)
        internal += struct.pack(">I", leaf_indices[0])
        for i in range(1, len(leaf_indices)):
            # Key is the first key of this leaf node
            first_key = leaf_nodes[i][0][0]
            key_idx = self.add_block(first_key)
            internal += struct.pack(">II", key_idx, leaf_indices[i])
        internal_idx = self.add_block(internal)

        tree_header = struct.pack(">4sIIIIBII", b"tree", 1, internal_idx,
                                  block_size, len(entries), 0, 0, 0)
        tree_idx = self.add_block(tree_header)
        self._named_blocks[name] = tree_idx
        return tree_idx

    def _build_leaf_node(self, entries: list[tuple[bytes, bytes]],
                         forward: int, backward: int,
                         block_size: int = 4096) -> bytes:
        """Build a leaf node from entries.

        Apple's BOM trees embed key data inline in the leaf node.  The layout
        is: header + entry pairs + padding to *block_size* + inline key data
        appended AFTER the block_size boundary.  The CoreUI tree reader reads
        block_size bytes for the entry section, then reads inline keys starting
        at offset block_size.  Getting this wrong causes value lookups to
        return garbage (e.g. garbled color components or multisize descriptors).
        """
        node = struct.pack(">HHII", 1, len(entries), forward, backward)
        key_data_list = []
        for key_data, value_data in entries:
            val_idx = self.add_block(value_data)
            key_idx = self.add_block(key_data)
            node += struct.pack(">II", val_idx, key_idx)
            key_data_list.append(key_data)
        # Pad entry section to exactly block_size
        if len(node) < block_size:
            node += b"\x00" * (block_size - len(node))
        # Append inline key data AFTER the block_size boundary
        for kd in key_data_list:
            node += kd
        return node

    def add_raw_key_tree(self, name: str,
                         entries: list[tuple[int, bytes]],
                         block_size: int = 1024) -> int:
        """Add a BOM tree where keys are raw uint32 values (not block refs).

        Used for BITMAPKEYS where the key is the facet identifier stored
        directly as a uint32 in the node entry.

        entries: list of (raw_key_uint32, value_bytes) pairs, sorted by key.
        """
        if not entries:
            return self.add_tree(name, [], block_size)

        # Build leaf node(s)
        # Entry format: value_block_idx(4) + raw_key(4) = 8 bytes
        max_per_node = (block_size - 12) // 8

        leaf_batches = []
        for start in range(0, len(entries), max_per_node):
            leaf_batches.append(entries[start:start + max_per_node])

        if len(leaf_batches) == 1:
            node = struct.pack(">HHII", 1, len(entries), 0, 0)
            for raw_key, value_data in entries:
                val_idx = self.add_block(value_data)
                node += struct.pack(">II", val_idx, raw_key)
            # Pad node to block_size (BOM reader may read full block_size)
            if len(node) < block_size:
                node += b"\x00" * (block_size - len(node))
            node_idx = self.add_block(node)
            tree_header = struct.pack(">4sIIIIBII", b"tree", 1, node_idx,
                                      block_size, len(entries), 1, 0, 0)
            tree_idx = self.add_block(tree_header)
            self._named_blocks[name] = tree_idx
            return tree_idx

        # Multiple leaf nodes — same two-pass approach
        leaf_indices = []
        for batch in leaf_batches:
            leaf_indices.append(self.add_block(b"\x00"))

        for i, batch in enumerate(leaf_batches):
            fwd = leaf_indices[i + 1] if i + 1 < len(leaf_indices) else 0
            bwd = leaf_indices[i - 1] if i > 0 else 0
            node = struct.pack(">HHII", 1, len(batch), fwd, bwd)
            for raw_key, value_data in batch:
                val_idx = self.add_block(value_data)
                node += struct.pack(">II", val_idx, raw_key)
            if len(node) < block_size:
                node += b"\x00" * (block_size - len(node))
            self._blocks[leaf_indices[i]] = node

        internal = struct.pack(">HHII", 0, len(leaf_indices) - 1, 0, 0)
        internal += struct.pack(">I", leaf_indices[0])
        for i in range(1, len(leaf_indices)):
            first_key = leaf_batches[i][0][0]
            internal += struct.pack(">II", first_key, leaf_indices[i])
        internal_idx = self.add_block(internal)

        tree_header = struct.pack(">4sIIIIBII", b"tree", 1, internal_idx,
                                  block_size, len(entries), 1, 0, 0)
        tree_idx = self.add_block(tree_header)
        self._named_blocks[name] = tree_idx
        return tree_idx

    def write(self, path: str):
        """Write the BOM file to disk."""
        MIN_TABLE_SIZE = 256  # Apple pre-allocates at least 256 entries
        FREELIST_SIZE = 20    # 20-byte freelist trailer (all zeros)

        # Calculate block offsets
        header_size = 32  # BOMStore header
        current_offset = header_size
        num_used_blocks = len(self._blocks)  # Max block index + 1 (includes null block 0)

        block_entries = []
        for i, block_data in enumerate(self._blocks):
            if i == 0:
                block_entries.append((0, 0))
                continue
            if current_offset % 16 != 0:
                current_offset += 16 - (current_offset % 16)
            block_entries.append((current_offset, len(block_data)))
            current_offset += len(block_data)

        # Pad block table to minimum size with null entries
        while len(block_entries) < MIN_TABLE_SIZE:
            block_entries.append((0, 0))

        # Build index (block table + freelist)
        index_data = struct.pack(">I", len(block_entries))
        for offset, length in block_entries:
            index_data += struct.pack(">II", offset, length)
        index_data += b"\x00" * FREELIST_SIZE  # Empty freelist

        # Build vars section
        vars_data = struct.pack(">I", len(self._named_blocks))
        for name, block_idx in self._named_blocks.items():
            name_bytes = name.encode("ascii")
            vars_data += struct.pack(">IB", block_idx, len(name_bytes))
            vars_data += name_bytes

        # Align current_offset for vars
        if current_offset % 16 != 0:
            current_offset += 16 - (current_offset % 16)
        vars_offset = current_offset
        vars_length = len(vars_data)
        current_offset += vars_length

        # Align for index
        if current_offset % 16 != 0:
            current_offset += 16 - (current_offset % 16)
        index_offset = current_offset
        index_length = len(index_data)

        # Write the file
        with open(path, "wb") as f:
            # Header
            f.write(b"BOMStore")
            f.write(struct.pack(">I", 1))  # version
            f.write(struct.pack(">I", num_used_blocks))  # numberOfBlocks (used only)
            f.write(struct.pack(">I", index_offset))
            f.write(struct.pack(">I", index_length))
            f.write(struct.pack(">I", vars_offset))
            f.write(struct.pack(">I", vars_length))

            # Blocks
            for i, block_data in enumerate(self._blocks):
                if i == 0:
                    continue
                expected_offset = block_entries[i][0]
                current_pos = f.tell()
                if current_pos < expected_offset:
                    f.write(b"\x00" * (expected_offset - current_pos))
                f.write(block_data)

            # Vars
            current_pos = f.tell()
            if current_pos < vars_offset:
                f.write(b"\x00" * (vars_offset - current_pos))
            f.write(vars_data)

            # Index
            current_pos = f.tell()
            if current_pos < index_offset:
                f.write(b"\x00" * (index_offset - current_pos))
            f.write(index_data)
