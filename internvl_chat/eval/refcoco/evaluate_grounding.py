import argparse
import itertools
import json
import os
import random
import re
import time
from functools import partial

import torch
from internvl.train.dataset import build_transform
from PIL import Image
from torchvision.ops.boxes import box_area
from tqdm import tqdm
from transformers import AutoTokenizer

ds_collections = {
    'refcoco_val': 'data/refcoco/refcoco_val.jsonl',
    'refcoco_testA': 'data/refcoco/refcoco_testA.jsonl',
    'refcoco_testB': 'data/refcoco/refcoco_testB.jsonl',
    'refcoco+_val': 'data/refcoco/refcoco+_val.jsonl',
    'refcoco+_testA': 'data/refcoco/refcoco+_testA.jsonl',
    'refcoco+_testB': 'data/refcoco/refcoco+_testB.jsonl',
    'refcocog_val': 'data/refcoco/refcocog_val.jsonl',
    'refcocog_test': 'data/refcoco/refcocog_test.jsonl',
}


def box_iou(boxes1, boxes2):
    area1 = box_area(boxes1)
    area2 = box_area(boxes2)

    lt = torch.max(boxes1[:, None, :2], boxes2[:, :2])  # [N,M,2]
    rb = torch.min(boxes1[:, None, 2:], boxes2[:, 2:])  # [N,M,2]

    wh = (rb - lt).clamp(min=0)  # [N,M,2]
    inter = wh[:, :, 0] * wh[:, :, 1]  # [N,M]

    union = area1[:, None] + area2 - inter

    iou = inter / union
    return iou, union


def collate_fn(batches, tokenizer):
    pixel_values = torch.cat([_['pixel_values'] for _ in batches], dim=0)
    texts = [_['text'] for _ in batches]
    bboxes = [_['bbox'] for _ in batches]
    hws = [_['hw'] for _ in batches]
    return pixel_values, texts, bboxes, hws


class RefCOCODataset(torch.utils.data.Dataset):

    def __init__(self, test, prompt, input_size=224, pad2square=False):
        self.datas = open(test).readlines()
        self.prompt = prompt
        self.transform = build_transform(is_train=False, input_size=input_size, pad2square=pad2square)

    def __len__(self):
        return len(self.datas)

    def __getitem__(self, idx):
        data = json.loads(self.datas[idx].strip())
        image = data['image']
        text = data['sent']
        bbox = data['bbox']

        w, h = data['width'], data['height']

        image = Image.open(image).convert('RGB')
        pixel_values = self.transform(image).unsqueeze(0)

        return {
            'text': self.prompt.format(text),
            'pixel_values': pixel_values,
            'bbox': bbox,
            'hw': (h, w),
        }


class InferenceSampler(torch.utils.data.sampler.Sampler):

    def __init__(self, size):
        self._size = int(size)
        assert size > 0
        self._rank = torch.distributed.get_rank()
        self._world_size = torch.distributed.get_world_size()
        self._local_indices = self._get_local_indices(size, self._world_size, self._rank)

    @staticmethod
    def _get_local_indices(total_size, world_size, rank):
        shard_size = total_size // world_size
        left = total_size % world_size
        shard_sizes = [shard_size + int(r < left) for r in range(world_size)]

        begin = sum(shard_sizes[:rank])
        end = min(sum(shard_sizes[:rank + 1]), total_size)
        return range(begin, end)

    def __iter__(self):
        yield from self._local_indices

    def __len__(self):
        return len(self._local_indices)


def evaluate_chat_model():
    print('prompt:', prompt)
    random.seed(args.seed)
    summaries = []

    for ds_name in args.datasets:
        dataset = RefCOCODataset(
            test=ds_collections[ds_name],
            prompt=prompt,
            input_size=image_size,
            pad2square=pad2square,
        )
        dataloader = torch.utils.data.DataLoader(
            dataset=dataset,
            sampler=InferenceSampler(len(dataset)),
            batch_size=args.batch_size,
            num_workers=args.num_workers,
            pin_memory=True,
            drop_last=False,
            collate_fn=partial(collate_fn, tokenizer=tokenizer),
        )

        outputs = []
        for _, (pixel_values, questions, bboxes, hws) in enumerate(tqdm(dataloader)):
            pixel_values = pixel_values.to(torch.bfloat16).cuda()
            generation_config = dict(
                num_beams=args.num_beams,
                max_new_tokens=100,
                min_new_tokens=1,
                length_penalty=1,
                do_sample=True if args.temperature > 0 else False,
                temperature=args.temperature,
            )
            pred = model.chat(
                tokenizer=tokenizer,
                pixel_values=pixel_values,
                question=questions[0],
                generation_config=generation_config,
            )
            answers = [pred]

            for bbox, hw, answer in zip(bboxes, hws, answers):
                outputs.append({
                    'answer': answer,
                    'gt_bbox': bbox,
                    'hw': hw,
                })

        torch.distributed.barrier()

        world_size = torch.distributed.get_world_size()
        merged_outputs = [None for _ in range(world_size)]
        torch.distributed.all_gather_object(merged_outputs, outputs)

        merged_outputs = [_ for _ in itertools.chain.from_iterable(merged_outputs)]

        if torch.distributed.get_rank() == 0:
            print(f'Evaluating {ds_name} ...')
            time_prefix = time.strftime('%y%m%d%H%M%S', time.localtime())
            results_file = f'{ds_name}_{time_prefix}.json'
            results_file = os.path.join(args.out_dir, results_file)
            json.dump(merged_outputs, open(results_file, 'w'))

            correct = total_cnt = 0
            for i, output in enumerate(merged_outputs):
                predict_bbox = re.findall(PATTERN, output['answer'])
                try:
                    predict_bbox = (float(predict_bbox[0][0]), float(predict_bbox[0][1]), float(predict_bbox[0][2]),
                                    float(predict_bbox[0][3]))
                except:
                    predict_bbox = (0., 0., 0., 0.)
                target_bbox = torch.tensor(output['gt_bbox'],
                                           dtype=torch.float32).view(-1, 4)
                predict_bbox = torch.tensor(predict_bbox,
                                            dtype=torch.float32).view(-1, 4)
                if predict_bbox.sum() >= 4:
                    predict_bbox = predict_bbox / 1000
                predict_bbox[:, 0::2] *= output['hw'][1]
                predict_bbox[:, 1::2] *= output['hw'][0]
                iou, _ = box_iou(predict_bbox, target_bbox)
                iou = iou.item()
                total_cnt += 1
                if iou >= 0.5:
                    correct += 1

            print(f'Evaluating {ds_name} ...')
            print(f'Precision @ 1: {correct / total_cnt} \n')
            summaries.append([args.checkpoint, ds_name, f'Precision @ 1: {correct / total_cnt} \n'])

        torch.distributed.barrier()

    out_path = '_'.join(args.checkpoint.split('/')[-2:])
    writer = open(os.path.join(args.out_dir, f'{out_path}.txt'), 'a')
    print(f"write results to file {os.path.join(args.out_dir, f'{out_path}.txt')}")
    for summary in summaries:
        print(summary)
        writer.write(f'{summary}\n')
    writer.close()


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', type=str, default='')
    parser.add_argument('--datasets', type=str, default='refcoco_val,refcoco_testA,refcoco_testB,'
                                                        'refcoco+_val,refcoco+_testA,refcoco+_testB,'
                                                        'refcocog_val,refcocog_test')
    parser.add_argument('--batch-size', type=int, default=1)
    parser.add_argument('--num-workers', type=int, default=1)
    parser.add_argument('--num-beams', type=int, default=5)
    parser.add_argument('--out-dir', type=str, default='results')
    parser.add_argument('--sample', type=bool, default=False)
    parser.add_argument('--temperature', type=float, default=0.0)
    parser.add_argument('--seed', type=int, default=0)
    args = parser.parse_args()

    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)

    args.datasets = args.datasets.split(',')
    print('datasets:', args.datasets)
    assert args.batch_size == 1, 'Only batch size 1 is supported'

    torch.distributed.init_process_group(
        backend='nccl',
        world_size=int(os.getenv('WORLD_SIZE', '1')),
        rank=int(os.getenv('RANK', '0')),
    )

    torch.cuda.set_device(int(os.getenv('LOCAL_RANK', 0)))

    tokenizer = AutoTokenizer.from_pretrained(args.checkpoint, trust_remote_code=True)
    PATTERN = re.compile(r'\[*\[(.*?),(.*?),(.*?),(.*?)\]\]*')

    if 'qllama' in args.checkpoint.lower():
        from internvl.model.internvl_chat_with_qllama import InternVLChatModel
        model = InternVLChatModel.from_pretrained(
            args.checkpoint, low_cpu_mem_usage=True, torch_dtype=torch.bfloat16).cuda().eval()
        image_size = model.internvl.config.force_image_size or model.config.internvl_config.vision_config.image_size
        pad2square = model.config.pad2square
        prompt = 'Please provide the bounding box coordinate of the region this sentence describes: {}'
    else:
        from internvl.model.internvl_chat import InternVLChatModel
        model = InternVLChatModel.from_pretrained(
            args.checkpoint, low_cpu_mem_usage=True, torch_dtype=torch.bfloat16).cuda().eval()
        image_size = model.config.force_image_size or model.config.vision_config.image_size
        pad2square = model.config.pad2square
        prompt = 'Please provide the bounding box coordinate of the region this sentence describes: <ref>{}</ref>'

    total_params = sum(p.numel() for p in model.parameters()) / 1e9
    if total_params > 30:
        args.num_beams = 1
        print(f'[test] total_params: {total_params}B, use num_beams: {args.num_beams}')
    else:
        print(f'[test] total_params: {total_params}B')
    print(f'[test] image_size: {image_size}')
    print(f'[test] pad2square: {pad2square}')
    print(f'[test] template: {model.config.template}')

    evaluate_chat_model()
