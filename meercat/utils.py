"""Utilities."""
import csv
import collections
import itertools
import json
import logging
import os
import pickle
import random

import torch
from tqdm import tqdm

logger = logging.getLogger(__name__)


ENTITY_VOCAB_FILENAME = 'entities.txt'
ENTITY_MENTION_START = '[E_START]'
ENTITY_MENTION_END = '[E_END]'


def add_mention_seps(
    tokenizer,
):
    """Adds entity mention seperator tokens if needed."""
    # start_id = tokenizer.convert_tokens_to_ids(ENTITY_MENTION_START)
    # if start_id == tokenizer.unk_token_id:
        # tokenizer.add_tokens(ENTITY_MENTION_START)
    # end_id = tokenizer.convert_tokens_to_ids(ENTITY_MENTION_END)
    # if end_id == tokenizer.unk_token_id:
        # tokenizer.add_tokens(ENTITY_MENTION_END)
    tokenizer.add_special_tokens({
        'additional_special_tokens': [ENTITY_MENTION_START, ENTITY_MENTION_END],
    })


class EntityTokenizer:
    def __init__(self, entities, counts):
        self.idx_to_entity = entities
        self.entity_to_idx = {x: i for i, x in enumerate(entities)}
        self.counts = counts

    def __len__(self):
        return len(self.idx_to_entity)

    def __call__(self, entity_id):
        if entity_id not in self.entity_to_idx:
            logger.warning('Adding entity to vocabulary: %s.', entity_id)
            self.entity_to_idx[entity_id] = len(self.entity_to_idx)
            self.idx_to_entity.append(entity_id)
        return self.entity_to_idx[entity_id]

    def save_pretrained(
        self,
        save_directory,
    ):
        assert os.path.isdir(save_directory), 'save_directory is not a directory.'
        output_path = os.path.join(save_directory, ENTITY_VOCAB_FILENAME)
        with open(output_path, 'w') as g:
            writer = csv.writer(g)
            for entity, count in zip(self.idx_to_entity, self.counts):
                writer.writerow((entity, count))

    @classmethod
    def from_pretrained(cls, path):
        if os.path.isdir(path):
            path = os.path.join(path, ENTITY_VOCAB_FILENAME)
        entities = []
        counts = []
        with open(path, 'r') as f:
            reader = csv.reader(f)
            for entity, count in reader:
                entities.append(entity)
                counts.append(int(count))
        return cls(entities, counts)


def _encode_mention(data, tokenizer):
    mention_tokens = tokenizer.tokenize(data['mention'])
    mention_tokens = [ENTITY_MENTION_START, *mention_tokens, ENTITY_MENTION_END]
    left_tokens = tokenizer.tokenize(data['left_context'])
    right_tokens = tokenizer.tokenize(data['right_context'])

    # Get a roughly centered window around the mention.
    context_size = tokenizer.model_max_length - len(mention_tokens) - 2
    left_size = right_size = context_size // 2
    if len(left_tokens) < left_size:
        right_size += left_size - len(left_tokens)
        left_size = len(left_tokens)
    if len(right_tokens) < right_size:
        left_size += right_size -  len(right_tokens)
        right_size = len(right_tokens)
    left_tokens = left_tokens[-left_size:]
    right_tokens = right_tokens[:right_size]
    tokens = left_tokens + mention_tokens + right_tokens
    mention_encoding = tokenizer.encode_plus(
        text=tokens,
        add_special_tokens=True,
        padding='max_length',
        truncation=True,
        return_tensors='pt',
    )
    mention_encoding = {k: v.squeeze(0) for k, v in mention_encoding.items()}
    return mention_encoding


class ELDataset(torch.utils.data.Dataset):
    def __init__(self, mention_encodings, labels=None):
        if labels is not None:
            assert len(mention_encodings) == len(labels)
        self._mention_encodings = mention_encodings
        self._labels = labels

    def __len__(self):
        return len(self._mention_encodings)

    def __getitem__(self, idx):
        out = self._mention_encodings[idx]
        if self._labels is not None:
            out['labels'] = self._labels[idx]
        return  out

    @classmethod
    def from_jsonl(
        cls,
        fname,
        tokenizer,
        entity_tokenizer,
    ):
        mention_encodings = []
        labels = []
        with open(fname, 'r') as f:
            for line in tqdm(f):
                data = json.loads(line)
                mention_encodings.append(_encode_mention(data, tokenizer))
                labels.append(entity_tokenizer(data['entity_id']))
        return cls(mention_encodings, labels)

    @classmethod
    def load(cls, fname):
        with open(fname, 'rb') as f:
            state_dict = pickle.load(f)
        return cls(**state_dict)

    def save(self, fname):
        state_dict = {
            'mention_encodings': self._mention_encodings,
            'labels': self._labels,
        }
        with open(fname, 'wb') as f:
            pickle.dump(state_dict, f)


def streaming_shuffle(iterable, chunk_size=32768):
    # TODO: Test that this works with multiprocessing
    chunks = [iter(iterable)] * chunk_size
    for chunk in itertools.zip_longest(*chunks, fillvalue=None):
        chunk = [x for x in chunk if x is not None]
        random.shuffle(chunk)
        for element in chunk:
            yield element


class ELIterableDataset(torch.utils.data.IterableDataset):
    def __init__(
            self,
            fname,
            tokenizer,
            entity_tokenizer,
            rank,
            world_size,
            shuffle=False,
    ):
        super().__init__()
        self._fname = fname
        self._tokenizer = tokenizer
        self._entity_tokenizer = entity_tokenizer
        self._rank = rank
        self._world_size = world_size
        self._shuffle = shuffle

    def __iter__(self):
        def generator():
            worker_info = torch.utils.data.get_worker_info()
            with open(self._fname, 'r') as f:
                iter_ = streaming_shuffle(f) if self._shuffle else f
                for i, line in enumerate(iter_):
                    # Ensures data isn't repeated across processes
                    if self._world_size is not None:
                        if (i % self._world_size) != self._rank:
                            continue
                    data = json.loads(line)
                    model_inputs = _encode_mention(data, self._tokenizer)
                    model_inputs['labels'] = self._entity_tokenizer(data['entity_id'])
                    yield model_inputs
        return generator()


class APIterableDataset(torch.utils.data.IterableDataset):
    def __init__(
        self,
        entity_ids,
        buckets,
        tokenizer,
        rank,
        world_size,
    ):
        super().__init__()
        self._entity_ids = entity_ids
        self._buckets = buckets
        self._tokenizer = tokenizer
        self._rank = rank
        self._world_size = world_size

    @classmethod
    def load(cls, fname, tokenizer, rank, world_size):
        entity_ids = collections.Counter()
        buckets = collections.defaultdict(list)
        with open(fname, 'r') as f:
            for line in f:
                data = json.loads(line)
                entity_ids[data['entity_id']] += 1
                buckets[data['entity_id']].append(data)
        return cls(entity_ids, buckets, tokenizer, rank, world_size)

    def __iter__(self):
        # Entities w/ more than 1 mention
        multi_mentions = [x for x, y in self._entity_ids.items() if y > 1]
        choices = list(self._entity_ids.keys())
        for i, pos_entity_id in enumerate(multi_mentions):
            # Ensures data isn't repeated across processes
            if self._world_size is not None:
                if (i % self._world_size) != self._rank:
                    continue
            # Select two mentions from positive entity bucket
            pos_1, pos_2 = random.sample(self._buckets[pos_entity_id], 2)

            # Select a negative entity id at random
            neg_entity_id = random.choice(choices)
            while neg_entity_id == pos_entity_id:
                neg_entity_id = random.choice(choices)

            neg = random.choice(self._buckets[neg_entity_id])

            pos_1 = _encode_mention(pos_1, self._tokenizer)
            pos_2 = _encode_mention(pos_2, self._tokenizer)
            neg = _encode_mention(neg, self._tokenizer)

            yield pos_1, pos_2, neg

