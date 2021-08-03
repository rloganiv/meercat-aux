r"""Fast dendrogram purity computation.

TODO(rloganiv): Write usage
"""
import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
from typing import List, Optional

from meercat import medmentions


@dataclass
class Node:
    uid: str
    parent: Optional['Node'] = None
    children: List['Node'] = field(default_factory=list, repr=False)
    histogram: Counter = field(default_factory=Counter)

    def __repr__(self):
        return f'Node(uid={self.uid}, parent={self.parent.uid}, ' \
               f'children=[{", ".join(c.uid for c in self.children)}])'


def traverse(node):
    queue = [node]
    while queue:
        node = queue.pop()
        queue.extend(node.children)
        yield node


def load_metadata(f):
    metadata = {}
    for document in medmentions.parse_pubtator(f):
        for i, mention in enumerate(document.mentions):
            uid = f'{document.pmid}_{i}'
            # Semantic types are guaranteed unique for the subset of data
            # we're working with
            metadata[uid] = {
                'semantic_type': mention.semantic_types[0],
                'entity_id': mention.entity_id,
            }
    return metadata 


def load_dendrogram(f):
    lookup = {}
    reader = csv.reader(f, delimiter='\t')
    for uid, parent_uid, label in reader:
        node = Node(uid=uid)
        lookup[uid] = node
        if parent_uid == 'None':
            root = node
        else:
            node.parent = lookup[parent_uid]
            node.parent.children.append(node)
        if label != 'None':
            node.histogram[label] += 1
    return root 


def accumulate_purity(root, metadata=None, cluster_by=None):
    summand = 0
    for node in reversed(list(traverse(root))):
        if node.children:
            # Get node's histogram
            for child in node.children:
                node.histogram.update(child.histogram)
            # Add purity contributions
            n_leaves = sum(node.histogram.values())
            # Note: Assuming tree is binary
            for key in node.histogram:
                pairs = node.children[0].histogram[key] * node.children[1].histogram[key]
                summand += pairs * node.histogram[key] / n_leaves
        else:
            # Get metadata for leaf node if not already added.
            if metadata is not None:
                cluster = metadata[node.uid][cluster_by]
                node.histogram[cluster] = 1
    # Compute normalization constant
    p_star = sum(x * (x - 1) / 2 for x in root.histogram.values())
    return summand / p_star


def main(args):
    if args.medmentions_path is not None:
        with open(args.medmentions_path, 'r') as f:
            metadata = load_metadata(f)
    else:
        metadata = None
    with open(args.dendrogram_path, 'r') as f:
        root = load_dendrogram(f)
    purity = accumulate_purity(root, metadata, args.cluster_by)
    print(f'Dendrogram Purity: {purity: 0.4f}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--dendrogram-path', type=str, required=True)
    parser.add_argument('-m', '--medmentions-path', type=str, default=None)
    parser.add_argument('-c', '--cluster-by', type=str,
                        choices=('semantic_type', 'entity_id'))
    args = parser.parse_args()

    main(args)

