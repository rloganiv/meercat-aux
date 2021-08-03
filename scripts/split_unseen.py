import argparse
import json


def main(args):
    train_entity_ids = set()
    with open(args.train, 'r') as f:
        for line in f:
            data = json.loads(line)
            train_entity_ids.add(data['entity_id'])

    seen = set()
    with open(args.eval, 'r') as f, \
         open(args.eval + '.seen', 'w') as g_seen, \
         open(args.eval + '.unseen', 'w') as g_unseen:
        for line in f:
            data = json.loads(line)
            if data['entity_id'] in train_entity_ids:
                g_seen.write(line)
                seen.add(data['entity_id'])
            else:
                g_unseen.write(line)
    print('Percent Overlap: %0.4f' % (len(seen) / len(train_entity_ids)))



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--train', type=str)
    parser.add_argument('--eval', type=str)
    args = parser.parse_args()

    main(args)

