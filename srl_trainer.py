import argparse
from tqdm import tqdm
import subprocess
import sys
import tempfile
from random import shuffle
import random

import numpy as np
import tensorflow as tf

from srl_data import PAD_INDEX
from srl_data import load_instances
from srl_data import load_model_files
from srl_reader import chunk
from srl_model import DBLSTMTagger

from tensorflow.contrib.crf import viterbi_decode

FLAGS = None


class DeepSrlTrainer(object):
    def __init__(self, flags):
        super(DeepSrlTrainer, self).__init__()
        self.save_path = flags.save
        self.load_path = flags.load
        self.max_epochs = 500
        self.batch_size = 80
        self.training_iterator = SrlDataIterator(load_instances(flags.train), self.batch_size)
        self.validation_iterator = SrlDataIterator(load_instances(flags.valid), self.batch_size)
        self.test_iterator = SrlDataIterator(load_instances(flags.test), self.batch_size)
        self.vectors, self.word_vocab, self.label_vocab, self.char_vocab = load_model_files(flags.vocab)
        self.emb_dim = self.vectors.shape[1]

        self.reverse_word_vocab = [None] * len(self.word_vocab)
        for key, val in self.word_vocab.iteritems():
            self.reverse_word_vocab[val] = key

        self.reverse_label_vocab = [None] * len(self.label_vocab)
        for key, val in self.label_vocab.iteritems():
            self.reverse_label_vocab[val] = key

        self.transition_params = create_transition_matrix(self.reverse_label_vocab)

        self.script_path = flags.script

    def train(self):
        with tf.Session() as sess:
            graph = DBLSTMTagger(vocab_size=len(self.word_vocab), char_vocab_size=len(self.char_vocab),
                                 emb_dim=self.emb_dim, num_layers=4, marker_dim=100, char_dim=32,
                                 state_dim=300, num_classes=len(self.label_vocab))
            graph.train()
            # tf.summary.FileWriter('data/logs/', sess.graph)
            print('Initializing variables...')
            if self.load_path:
                graph.saver.restore(sess, self.load_path)
            else:
                sess.run(tf.global_variables_initializer())
                graph.initialize_embeddings(sess, self.vectors)

            current_epoch, step, max_score = 0, 0, float('-inf')
            while current_epoch < self.max_epochs:
                print('Epoch %s' % current_epoch)
                with tqdm(total=self.training_iterator.size, leave=False, unit=' instances') as bar:
                    for batch in self.training_iterator.epoch():
                        feed = {graph.feed_dict[k]: batch[k] for k in batch.keys()}
                        feed[graph.feed_dict['keep_prob']] = 0.9
                        sess.run(graph.train_step, feed_dict=feed)
                        step += 1
                        bar.update(len(batch['labels']))

                pred_ys, gold_ys, words, indices = [], [], [], []
                with tqdm(total=self.validation_iterator.size, leave=False, unit=' instances') as bar:
                    for batch in self.validation_iterator.epoch():
                        feed = {graph.feed_dict[k]: batch[k] for k in batch.keys()}
                        feed[graph.feed_dict['keep_prob']] = 1.0
                        logits = sess.run(graph.scores, feed_dict=feed)

                        gold_ys.extend([gold[:stop] for (gold, stop) in zip(batch['labels'], batch['lengths'])])
                        pred_ys.extend(
                            [viterbi_decode(score=pred[:stop], transition_params=self.transition_params)[0] for
                             (pred, stop) in zip(logits, batch['lengths'])])
                        words.extend(batch['words'])
                        indices.extend(batch['markers'])
                        bar.update(len(batch['labels']))

                score = self.evaluate(words, pred_ys, gold_ys, indices)
                if score > max_score:
                    max_score = score
                    if self.save_path:
                        save_path = graph.saver.save(sess, self.save_path)
                        print("Model saved in file: %s" % save_path)

                print('Epoch {} F1: {} (best: {})'.format(current_epoch, score, max_score))
                current_epoch += 1

    def evaluate(self, words, pred_ys, gold_ys, indices):
        with tempfile.NamedTemporaryFile() as gold_temp, tempfile.NamedTemporaryFile() as pred_temp:
            self._write_to_file(gold_temp, words, gold_ys, indices)
            self._write_to_file(pred_temp, words, pred_ys, indices)
            result = subprocess.check_output(['perl', self.script_path, gold_temp.name, pred_temp.name]).decode('utf-8')
            print(result)
            return float(result.strip().split('\n')[6].strip().split()[6])

    def _write_to_file(self, output_file, xs, ys, indices):
        for words, labels, markers in zip(xs, ys, indices):
            line = ''
            for word, predicted, marker in zip(
                    words, chunk([self.reverse_label_vocab[l] for l in labels], conll=True), markers):
                line += '{} {}\n'.format(marker == 1 and word or '-', predicted)
            output_file.write(line + '\n')
        output_file.flush()
        output_file.seek(0)


def create_transition_matrix(labels):
    num_tags = len(labels)
    transition_params = np.zeros([num_tags, num_tags], dtype=np.float32)
    for i, prev_label in enumerate(labels):
        for j, label in enumerate(labels):
            if i != j and label[0] == 'I' and not prev_label == 'B' + label[1:]:
                transition_params[i, j] = np.NINF
    return transition_params


class SrlDataIterator(object):
    def __init__(self, data, batch_size, pad_index=PAD_INDEX, num_buckets=5):
        super(SrlDataIterator, self).__init__()
        self.num_buckets = num_buckets
        self.pad_index = pad_index
        self.batch_size = batch_size
        self.size = len(data)
        data.sort(key=lambda inst: inst['length'])
        self.bucket_size = self.size / num_buckets
        self.data = []
        for bucket in range(num_buckets):
            self.data.append(data[bucket * self.bucket_size: (bucket + 1) * self.bucket_size])
        self.data[-1].extend(data[self.bucket_size * num_buckets:])  # add remaining instances
        self.pointer = np.array([0] * num_buckets)

    def max_steps(self):
        return len(self.data) / self.batch_size

    def epoch(self):
        self._reset()
        while not self._has_next():
            # select a random bucket (from remaining buckets)
            bucket = random.choice([i for (i, p) in enumerate(self.pointer.tolist()) if p + 1 < self.bucket_size])

            batch = self.data[bucket][self.pointer[bucket]:self.pointer[bucket] + self.batch_size]
            self.pointer[bucket] += len(batch)
            yield self._prepare_batch(batch)

    def _has_next(self):
        # noinspection PyTypeChecker
        return np.all(self.pointer >= self.bucket_size)

    def _reset(self):
        for i in range(self.num_buckets):
            shuffle(self.data[i])
            self.pointer[i] = 0

    def _prepare_batch(self, batch):
        lengths = [instance['length'] for instance in batch]
        max_length = max(lengths)
        labels = self._pad_vals('labels', batch, max_length)
        words = self._pad_vals('words', batch, max_length)
        chars = self._pad_list_feature('chars', batch, max_length)
        markers = self._pad_vals('is_predicate', batch, max_length)
        return {'labels': labels, 'words': words, 'markers': markers, 'lengths': lengths, 'chars': chars}

    def _pad_vals(self, key, batch, maxlen):
        padded = np.empty([len(batch), maxlen], dtype=np.int32)
        padded.fill(self.pad_index)
        for i, sentence in enumerate(padded):
            sentence[:batch[i]['length']] = batch[i][key]
        return padded

    def _pad_list_feature(self, key, batch, maxlen):
        padded = np.empty([len(batch), maxlen, 15], dtype=np.int32)
        padded.fill(self.pad_index)
        for i, sentence in enumerate(padded):
            features = batch[i][key]
            for index, word in enumerate(features):
                sentence[index, :word.size] = word[:15]
        return padded


def main(_):
    srl_trainer = DeepSrlTrainer(FLAGS)
    srl_trainer.train()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--save', type=str, default='data/models/model', help='Path to save models/checkpoints.')
    parser.add_argument('--load', type=str, help='Path to load previously saved model.')
    parser.add_argument('--train', required=True, type=str, help='Binary (*.pkl) train file path.')
    parser.add_argument('--valid', required=True, type=str, help='Binary (*.pkl) validation file path.')
    parser.add_argument('--test', required=True, type=str, help='Binary (*.pkl) test file path.')
    parser.add_argument('--vocab', required=True, type=str, help='Path to directory containing vocabulary files.')
    parser.add_argument('--script', required=True, type=str, help='Path to evaluation script.')

    FLAGS, unparsed = parser.parse_known_args()
    tf.app.run(main=main, argv=[sys.argv[0]] + unparsed)
