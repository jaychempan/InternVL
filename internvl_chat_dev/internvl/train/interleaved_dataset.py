import hashlib
import io
import json
import os
import random
import time
from copy import deepcopy

import torch
from internvl.train.constants import (IMG_CONTEXT_TOKEN, IMG_END_TOKEN,
                                      IMG_START_TOKEN)
from internvl.train.dataset import build_transform
from petrel_client.client import Client as PetrelClient
from PIL import Image
from torch.utils.data import Dataset
from transformers import AutoTokenizer


def load_image(image_url_or_path, _client=None, convert_rgb: bool = True):
    if 's3://' in image_url_or_path:
        image = Image.open(io.BytesIO(_client.get(image_url_or_path)))
    else:
        image = Image.open(image_url_or_path)
    return image.convert('RGB') if convert_rgb else image


def load_json(json_url_or_path, _client=None):
    if 's3://' in json_url_or_path:
        try_times = 0
        bytes = None
        while try_times < 10:
            try:
                bytes = _client.get(json_url_or_path)
                break
            except Exception as e:
                print(f'Failed to get {json_url_or_path}, retry {try_times}')
                try_times += 1
                time.sleep(1)
        return json.load(io.BytesIO(bytes))
    else:
        return json.load(open(json_url_or_path, 'r'))


def load_json_line(line_str, try_times=20):
    _try_times = 0
    while _try_times < try_times:
        try:
            data = json.loads(line_str)
            break
        except Exception as e:
            data = None
            print(f'Failed to load line, retry {_try_times}')
            _try_times += 1
            line_str = line_str[:-1]
    if data is None:
        raise Exception(f'Failed to get {line_str}')
    return data


def load_jsonl(jsonl_url_or_path, _client=None):
    if 's3://' in jsonl_url_or_path:
        try_times = 0
        while try_times < 10:
            try:
                bytes = _client.get(jsonl_url_or_path)
                break
            except Exception as e:
                print(f'Failed to get {jsonl_url_or_path}, retry {try_times}')
                try_times += 1
                time.sleep(1)
        lines = io.BytesIO(bytes).readlines()
    else:
        lines = open(jsonl_url_or_path, 'r').readlines()

    data = []
    for line in lines:
        if len(line.strip()) > 2:
            try:
                sample = load_json_line(line)
                data.append(sample)
            except Exception as e:
                raise ValueError(f'Failed to load line: {line}') from e
    return data


def encode_hash_sha256(txt):
    hash_object = hashlib.sha256(txt.encode())
    hex_dig = hash_object.hexdigest()
    return hex_dig


def partition_for_rank(all_rank_item_list: list, rank: int, world_num: int) -> list:
    this_rank_item_list = []
    this_rank_index = range(rank, len(all_rank_item_list), world_num)
    for idx in this_rank_index:
        this_rank_item_list.append(all_rank_item_list[idx])
    return this_rank_item_list


class InterleavedDataset(Dataset):

    def __init__(self, meta, tokenizer, tcs_loader, num_image_token=256, image_size=448, is_train=False,
                 pad2square=False, group_by_length=False, normalize_type='imagenet', max_num_images=6,
                 train_num_samples=None, dataset_resampled=True, seed=42):

        self.tokenizer = tokenizer
        self.data_path = meta['annotation']
        self.image_path = meta['root']
        self.max_num_images = max_num_images
        self.train_num_samples = train_num_samples
        self.dataset_resampled = dataset_resampled
        self.tcs_loader = tcs_loader
        self.num_image_token = num_image_token
        self.image_size = image_size
        self.is_train = is_train
        self.pad2square = pad2square
        self.group_by_length = group_by_length
        self.normalize_type = normalize_type

        # 0-6143 each 34195 samples
        self.shard_mode = True
        self.num_samples_each_shard = 34190  # even if the actual num is more
        self._length = self.num_samples_each_shard * 6144

        self.random = random.Random(seed)
        shard_order = list(range(6144))
        shard_order = partition_for_rank(shard_order, rank=0, world_num=1)
        if self.dataset_resampled:
            self.random.shuffle(shard_order)
        self.shard_order = shard_order

        # hard code a shard_id_range
        self.shard_id_range = {
            f'data0417_shuffled_shard_{shard_order[i]}.jsonl': (
                self.num_samples_each_shard * i,
                self.num_samples_each_shard * (i + 1) - 1
            )
            for i in range(len(shard_order))
        }

        self.current_shard_name = f'data0417_shuffled_shard_{shard_order[0]}.jsonl'
        print(f'Initialize shard file to {self.current_shard_name}')
        self.current_shard_data = load_jsonl(os.path.join(self.data_path, self.current_shard_name), self.tcs_loader)
        self.random.shuffle(self.current_shard_data)

    def load_ann_file(self, file_path):
        if file_path.endswith('.json'):
            return load_json(file_path, self.tcs_loader)
        elif file_path.endswith('.jsonl'):
            return load_jsonl(file_path, self.tcs_loader)
        else:
            raise NotImplementedError(f'Unsupported annotation file format: {file_path}')

    def __len__(self):
        if self.train_num_samples is not None:
            return min(self.train_num_samples, self._length) // self.data_args.world_size
        return self._length

    @staticmethod
    def check_shard_id_range(shard_id_range, length):
        ids = []
        for start, end in shard_id_range.values():
            ids.extend(range(start, end))
        assert sorted(ids)[:length] == list(range(0, length))

    def load_data(self, index):
        assert self.shard_mode
        if index >= self._length:
            index = index % self._length
        start, end = self.shard_id_range[self.current_shard_name]
        if start <= index <= end:
            return deepcopy(self.current_shard_data[index - start])
        else:
            for shard_name, (start, end) in self.shard_id_range.items():
                if start <= index < end:
                    self.current_shard_name = shard_name
                    self.current_shard_data = self.load_ann_file(
                        os.path.join(self.data_path, shard_name))
                    self.random.shuffle(self.current_shard_data)
                    print(f'Change shard file to {self.current_shard_name}')
                    return deepcopy(self.current_shard_data[index - start])

    def get_img_filename(self, web_url):
        return self.encode_hash_sha256(web_url)

    @staticmethod
    def encode_hash_sha256(web_url):
        hash_object = hashlib.sha256(web_url.encode())
        hex_dig = hash_object.hexdigest()
        return hex_dig

    def load_image(self, image_path_or_url):
        try:
            if 's3://' in self.image_path:
                # load from aws ceph
                return Image.open(io.BytesIO(self.tcs_loader.get(image_path_or_url))).convert('RGB')
            else:
                # load from local (or s3mount node)
                return Image.open(image_path_or_url).convert('RGB')
        except Exception as err:
            print(f'Error loading image: {image_path_or_url}: {err}')
            return None

    def parse_sample(self, sample):
        images = sample['images']
        texts = sample['texts']
        metadata = sample.get(
            'metadata',
            [
                {'filename': self.encode_hash_sha256(web_url)}
                if web_url is not None else None
                for web_url in images
            ]
        )
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        assert isinstance(metadata, list), metadata
        valid_image = sample.get('valid_image', [True] * sum(img is not None for img in images))
        assert len(images) == len(texts)
        assert sum(img is not None for img in images) == sum(txt is None for txt in texts) == len(valid_image), (
            sum(img is not None for img in images), sum(txt in ['<image>', None] for txt in texts), len(valid_image),
            sample)
        for _img, _imgmeta in zip(images, metadata):
            assert (_img is None) == (_imgmeta is None), sample
        return images, texts, metadata, valid_image

    def preprocess_image(self, images):
        transform = build_transform(is_train=self.is_train, input_size=self.image_size,
                                    pad2square=self.pad2square, normalize_type=self.normalize_type)
        images = [transform(image) for image in images]
        images = torch.stack(images, dim=0)
        return images

    def getitem(self, index):
        # dict_keys(['general_metadata', 'images', 'texts', 'metadata', 'doc_loc'])
        sample = self.load_data(index)
        # parse sample and check
        images, texts, metadata, valid_image = self.parse_sample(sample)
        # get valid images
        images = [os.path.join(self.image_path, self.get_img_filename(img)) for img, _ in
                  zip(images, metadata) if img is not None]

        loaded_images = []
        valid_count = 0
        for idx, (img, valid) in enumerate(zip(images, valid_image)):
            if valid:
                if valid_count >= self.max_num_images:
                    valid_image[idx] = False
                else:
                    _image = self.load_image(img)
                    if _image is not None:
                        loaded_images.append(_image)
                        valid_count += 1
                    else:
                        valid_image[idx] = False
        images = loaded_images

        assert len(images) > 0 and sum(valid_image)

        image_idx = 0
        for i in range(len(texts)):
            if texts[i] is None:
                if valid_image[image_idx]:
                    texts[i] = '<image>'
                image_idx += 1
        text = '\n\n'.join([_ for _ in texts if _])
        # format cleanup
        text = text.replace('<image>\n\n', '<image>').replace('\n\n<image>', '<image>')
        image_tokens = f'{IMG_START_TOKEN}{IMG_CONTEXT_TOKEN * self.num_image_token}{IMG_END_TOKEN}'
        text = text.replace('<image>', image_tokens, len(images))
        tokenized = self.tokenizer(
            text,
            max_length=tokenizer.model_max_length,
            truncation=True,
            padding=False,
            return_tensors='pt',
        )
        pixel_values = self.preprocess_image(images)
        num_patches = pixel_values.size(0)
        input_ids = tokenized['input_ids']
        labels = input_ids.clone()
        image_start_token_id = tokenizer.convert_tokens_to_ids(IMG_START_TOKEN)
        image_end_token_id = tokenizer.convert_tokens_to_ids(IMG_END_TOKEN)
        image_context_token_id = tokenizer.convert_tokens_to_ids(IMG_CONTEXT_TOKEN)
        assert (labels == image_context_token_id).sum() == self.num_image_token * len(images), 'image tokens are truncated'
        labels[labels == image_start_token_id] = -100
        labels[labels == image_end_token_id] = -100
        labels[labels == image_context_token_id] = -100
        ret = dict(
            input_ids=input_ids[0],
            labels=labels[0],
            attention_mask=input_ids[0].ne(tokenizer.pad_token_id),
            pixel_values=pixel_values,
            image_flags=torch.tensor([1] * num_patches, dtype=torch.long)
        )
        return ret

    def __getitem__(self, index):
        while True:
            try:
                item = self.getitem(index)
                break
            except Exception as err:
                print(err)
                index = (index + 1) % len(self)
                print(f'Try to load next index: {index}')
        return item


if __name__ == '__main__':
    import argparse

    args = argparse.ArgumentParser()
    args.rank = 0
    args.world_size = 1
    args.batch_size_mmc4 = 1
    args.workers = 1
    model_path = '/mnt/petrelfs/wangwenhai/workspace/InternVL-release/internvl_chat_dev/release/InternVL-Chat-V1-5'
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True, use_fast=True)
    client = PetrelClient('~/petreloss.conf')

    metas = {
        'lmm_interleaved_data0417_shuffled': {
            'root': 'wwhnew_pssd:s3://mllm-cc/raw-images/',
            'annotation': 'langchao:s3://liqingyun/projects/lmm_interleaved/data0417_shuffled/',
            'data_augment': True,
            'repeat_time': 1,
            'length': 210063360
        },
    }
    dataset = InterleavedDataset(meta=metas['lmm_interleaved_data0417_shuffled'],
                                 tokenizer=tokenizer,
                                 tcs_loader=client,
                                 )
    item = dataset.__getitem__(0)
    print(item)
    print(f'length: {len(dataset)}')