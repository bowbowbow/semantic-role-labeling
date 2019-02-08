import logging
import os
import random
import time

import numpy as np
import tensorflow as tf
from tensorflow.contrib.crf import viterbi_decode
from tqdm import tqdm

from srl.common.constants import END_INDEX, KEEP_PROB_KEY, LABEL_KEY, LENGTH_KEY, PAD_INDEX, START_INDEX, INSTANCE_INDEX, \
    SENTENCE_INDEX
from srl.common.srl_utils import deserialize, read_json
from srl.data.features import get_features_from_config
from srl.model.tagger import DBLSTMTagger


class TaggerTrainer(object):
    def __init__(self, flags, extractor):
        super(TaggerTrainer, self).__init__()
        self.save_path = flags.save
        self.load_path = flags.load
        self.script_path = flags.script
        self.output_file = flags.output

        self._read_conf(flags.config)
        self.features = get_features_from_config(flags.config)

        self.extractor = extractor(feats=self.features)
        self.extractor.load(flags.vocab)

        self.label_vocab = self.extractor.extractors[LABEL_KEY].indices
        self.reverse_label_vocab = {index: label for label, index in self.label_vocab.items()}
        self.transition_params = TaggerTrainer._create_transition_matrix(
            [self.reverse_label_vocab[i] for i in range(len(self.label_vocab))])

        if flags.train:
            self.training_iterator = BatchIterator(deserialize(flags.train), self.batch_size, self.features,
                                                   num_buckets=self.num_buckets, max_length=self.max_length, end_pad=self.crf)
        if flags.valid:
            self.validation_iterator = BatchIterator(deserialize(flags.valid), self.batch_size, self.features, end_pad=self.crf,
                                                     preserve_order=True)
        if flags.test:
            self.test_iterator = BatchIterator(deserialize(flags.test), self.batch_size, self.features, end_pad=self.crf,
                                               preserve_order=True)

        self.sess = None
        self.graph = None

    def train(self):
        self.sess = tf.Session()
        print('=' * 30 + 'train(step2)' + '=' * 30)
        with self.sess as sess:
            print('=' * 30 + 'load_graph(step3)' + '=' * 30)
            self.graph = self._load_graph()
            self.graph.train()
            if self.load_path:
                self._restore(sess)
            else:
                print('=' * 30 + 'graph.initialize_embeddings(step6)' + '=' * 30)
                sess.run(tf.global_variables_initializer())
                self.graph.initialize_embeddings(sess)

            print('=' * 30 + 'start epochs(step7)' + '=' * 30)
            current_epoch, step, max_score = self.graph.global_step.eval() + 1, 0, float('-inf')
            patience = 0
            while current_epoch <= self.max_epochs:
                logging.info('Epoch %d', current_epoch)
                then = time.time()
                with tqdm(total=self.training_iterator.size, leave=False, unit=' instances') as bar:
                    for batch in self.training_iterator.epoch():
                        print('batch :', batch)

                        feed = {self.graph.feed_dict[k]: batch[k] for k in batch.keys() if k in self.graph.feed_dict}
                        feed[self.graph.feed_dict[KEEP_PROB_KEY]] = self.keep_prob
                        sess.run(self.graph.train_step, feed_dict=feed)
                        step += 1
                        bar.update(len(batch[LABEL_KEY]))
                sess.run(self.graph.global_step_increment)  # increment global step variable associated with graph
                logging.info('Training for epoch %d completed in %f seconds.', current_epoch, time.time() - then)

                if current_epoch % self.eval_every == 0:
                    score = self._test(iterator=self.validation_iterator)
                    if score > max_score:
                        max_score = score
                        patience = 0
                        if self.save_path:
                            logging.info("Saving model to %s" % os.path.normpath(self.save_path))
                            self.graph.saver.save(sess, self.save_path, global_step=self.graph.global_step)
                    else:
                        patience += 1

                    logging.info('Epoch %d F1: %f (best: %f, %d epoch(s) ago)', current_epoch, score, max_score, patience)

                current_epoch += 1

    def test(self):
        self.sess = tf.Session()
        with self.sess as sess:
            self._load_for_test(sess)
            self._test(self.test_iterator)

    def _test(self, iterator):
        raise NotImplementedError

    def predict(self, targets):
        if not self.sess:
            self.sess = tf.Session()
            self._load_for_test(self.sess)
        instances = self.extractor.read_instances(targets)
        iterator = BatchIterator(instances, 1, self.features, end_pad=self.crf, preserve_order=True)
        return self._predict(iterator)

    def _predict(self, iterator):
        raise NotImplementedError

    def _load_for_test(self, sess):
        self.graph = self._load_graph()
        self.graph.test()
        self._restore(sess)

    def _restore(self, sess):
        self.load_path = os.path.abspath(os.path.normpath(self.load_path))
        logging.info('Loading most recent checkpoint from %s', self.load_path)
        self.graph.saver.restore(sess, tf.train.latest_checkpoint(self.load_path))

    def _decode(self, predictions, stop, convert=False):
        raw = viterbi_decode(score=predictions[:stop], transition_params=self.graph.transition_matrix())[0]
        if convert:
            raw = [self.reverse_label_vocab[label] for label in raw]
        return raw

    def _logits(self, batch):
        feed = {self.graph.feed_dict[k]: batch[k] for k in batch.keys() if k in self.graph.feed_dict}
        for key in self.graph.dropout_keys:
            feed[self.graph.feed_dict[key]] = 1.0
        return self.sess.run(self.graph.scores, feed_dict=feed)

    def _read_conf(self, conf_json):
        conf = read_json(conf_json)
        logging.info(conf)
        self.crf = conf.get('crf', False)
        self.max_epochs = conf.get('max_epochs', 999)
        self.batch_size = conf.get('batch_size', 64)
        self.keep_prob = conf.get('keep_prob', 1)
        self.lstm_hidden_dim = conf.get('lstm_hidden_dim', 128)
        self.lstm_num_layers = conf.get('lstm_num_layers', 2)
        self.max_length = conf.get('max_length', 100)
        self.num_buckets = conf.get('num_buckets', 100)
        self.dblstm = conf.get('dblstm', False)
        self.orthonormal_init = conf.get('orthonormal_init', True)
        self.recurrent_dropout = conf.get('recurrent_dropout', True)
        self.highway = conf.get('highway', True)
        self.eval_every = conf.get('eval_every', 1)

    def _load_graph(self):
        return DBLSTMTagger(features=self.features, num_classes=len(self.label_vocab), num_layers=self.lstm_num_layers,
                            state_dim=self.lstm_hidden_dim, transition_params=self.transition_params,
                            crf=self.crf, dblstm=self.dblstm,
                            orthonormal_init=self.orthonormal_init,
                            recurrent_dropout=self.recurrent_dropout,
                            highway=self.highway)

    @staticmethod
    def _create_transition_matrix(labels):
        num_tags = len(labels)
        transition_params = np.zeros([num_tags, num_tags], dtype=np.float32)
        for i, prev_label in enumerate(labels):
            for j, label in enumerate(labels):
                if i != j and label[0] == 'I' and not prev_label == 'B' + label[1:]:
                    transition_params[i, j] = np.NINF
        return transition_params


class BatchIterator(object):
    def __init__(self, data, batch_size, features, num_buckets=5, max_length=99999, end_pad=False, preserve_order=False):
        super(BatchIterator, self).__init__()
        self.num_buckets = num_buckets
        self.batch_size = batch_size
        self.size = len(data)
        self.features = features
        self.end_pad = 1 if end_pad else 0
        self.preserve_order = preserve_order

        data = [x for x in data if x[LENGTH_KEY] <= max_length]
        if not self.preserve_order:
            data.sort(key=lambda inst: inst[LENGTH_KEY])
        else:
            self.num_buckets = 1
        self.bucket_size = self.size // self.num_buckets
        self.data = []
        for bucket in range(self.num_buckets):
            self.data.append(data[bucket * self.bucket_size: (bucket + 1) * self.bucket_size])
        self.data[-1].extend(data[self.bucket_size * self.num_buckets:])  # add remaining instances
        self.pointer = np.array([0] * self.num_buckets)

    def max_steps(self):
        return len(self.data) // self.batch_size

    def epoch(self):
        self._reset()
        while not self._has_next():
            # select a random bucket (from remaining buckets)
            bucket = random.choice([i for (i, p) in enumerate(self.pointer.tolist()) if p + 1 < self.bucket_size])

            batch = self.data[bucket][self.pointer[bucket]:self.pointer[bucket] + self.batch_size]
            if len(batch) == 0:
                break
            self.pointer[bucket] += len(batch)
            yield self._prepare_batch(batch)

    def _has_next(self):
        # noinspection PyTypeChecker
        return np.all(self.pointer >= self.bucket_size)

    def _reset(self):
        for i in range(self.num_buckets):
            if not self.preserve_order:
                random.shuffle(self.data[i])
            self.pointer[i] = 0

    def _prepare_batch(self, batch):
        lengths = [instance[LENGTH_KEY] + self.end_pad for instance in batch]
        indices = [instance.get(INSTANCE_INDEX, 0) + self.end_pad for instance in batch]
        sentence_indices = [instance.get(SENTENCE_INDEX, 0) + self.end_pad for instance in batch]
        max_length = max(lengths)  # minimum length 2 due to https://github.com/tensorflow/tensorflow/issues/7751
        labels = self._pad_2d(LABEL_KEY, batch, max_length)
        feed_dict = {SENTENCE_INDEX: sentence_indices, INSTANCE_INDEX: indices, LABEL_KEY: labels, LENGTH_KEY: lengths}
        for feature in self.features:
            if feature.rank == 2:
                feed_dict[feature.name] = self._pad_2d(feature.name, batch, max_length)
            elif feature.rank == 3:
                feed_dict[feature.name] = self._pad_3d(feature, batch, max_length, feature.function.max_length)
            elif feature.rank == 4:
                feed_dict[feature.name] = self._pad_4d(feature, batch, max_length, feature.function.max_length)
            if feature.keep_prob < 1:
                feed_dict[feature.name + KEEP_PROB_KEY] = feature.keep_prob
        return feed_dict

    @staticmethod
    def _pad_2d(key, batch, max_tokens):
        padded = np.empty([len(batch), max_tokens], dtype=np.int32)
        padded.fill(PAD_INDEX)
        for i, sentence in enumerate(padded):
            sentence[:batch[i][LENGTH_KEY]] = batch[i][key]
        return padded

    @staticmethod
    def _pad_3d(feature, batch, max_tokens, max_feat_length):
        padded_batch = np.empty([len(batch), max_tokens, max_feat_length], dtype=np.int32)
        padded_batch.fill(PAD_INDEX)
        for i_sentence, sentence in enumerate(padded_batch):
            phrases = batch[i_sentence][feature.name]
            for i_phrase, phrase in enumerate(phrases):
                sentence[i_phrase, 0:feature.left_padding] = START_INDEX
                end_word = phrase.size + feature.left_padding
                sentence[i_phrase, feature.left_padding:end_word] = phrase[:max_feat_length - feature.left_padding]
                sentence[i_phrase, end_word:end_word + feature.right_padding] = END_INDEX
        return padded_batch

    @staticmethod
    def _pad_4d(feature, batch, max_tokens, max_feat_length):
        max_sub_tokens = 0
        for sentence in batch:
            for phrase in sentence[feature.name]:
                max_sub_tokens = max(max_sub_tokens, len(phrase))
        padded_batch = np.empty([len(batch), max_tokens, max_sub_tokens, max_feat_length], dtype=np.int32)
        padded_batch.fill(PAD_INDEX)
        for i_sentence, sentence in enumerate(padded_batch):
            phrases = batch[i_sentence][feature.name]
            for i_phrase, phrase in enumerate(phrases):
                for i_word, word in enumerate(phrase):
                    sentence[i_phrase, i_word, 0:feature.left_padding] = START_INDEX
                    end_word = word.size + feature.left_padding
                    sentence[i_phrase, i_word, feature.left_padding:end_word] = word[:max_feat_length - feature.left_padding]
                    sentence[i_phrase, i_word, end_word:end_word + feature.right_padding] = END_INDEX
        return padded_batch
