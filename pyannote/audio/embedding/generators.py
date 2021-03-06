#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2016-2017 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr
# Grégory GELLY


import h5py
import os.path
import numpy as np
from pyannote.generators.batch import BaseBatchGenerator
from pyannote.generators.indices import random_label_index
from pyannote.audio.generators.labels import FixedDurationSequences
from pyannote.audio.embedding.callbacks import UpdateGeneratorEmbedding


class SequenceGenerator(object):
    """

    Parameters
    ----------
    robust: bool, optional
        When True, skip files for which feature extraction fails.

    """

    def __init__(self, feature_extractor, file_generator,
                 duration=5.0, min_duration=None, step=None,
                 heterogeneous=False, per_label=3, cache=None, robust=False):

        super(SequenceGenerator, self).__init__()

        self.feature_extractor = feature_extractor
        self.file_generator = file_generator
        self.duration = duration
        self.min_duration = min_duration
        self.step = step
        self.heterogeneous = heterogeneous
        self.per_label = per_label
        self.cache = cache
        self.robust = robust

        self.generator_ = FixedDurationSequences(
            self.feature_extractor,
            duration=self.duration,
            min_duration=self.min_duration,
            step=self.step, heterogeneous=self.heterogeneous,
            batch_size=1 if self.cache else -1)

        # there is no need to cache preprocessed features
        # as the generator is iterated only once
        self.generator_.cache_preprocessed_ = False

        self.sequence_generator_ = self.iter_sequences(cache=self.cache)

        # consume first element of generator
        # this is meant to pre-generate all labeled sequences once and for all
        # and also to precompute the number of unique labels
        next(self.sequence_generator_)

    def _precompute(self, Xy_generator, cache):

        with h5py.File(cache, mode='w') as fp:

            # initialize with a fixed number of sequences
            n_sequences = 1000

            y = fp.create_dataset(
                'y', shape=(n_sequences, ),
                dtype=h5py.special_dtype(vlen=bytes),
                maxshape=(None, ))

            for i, (X_, y_) in enumerate(Xy_generator):

                if i == 0:
                    _, n_samples, n_features = X_.shape
                    X = fp.create_dataset(
                        'X', dtype=X_.dtype, compression='gzip',
                        shape=(n_sequences, n_samples, n_features),
                        chunks=(1, n_samples, n_features),
                        maxshape=(None, n_samples, n_features))

                # increase number of sequences on demand
                if i == n_sequences:
                    n_sequences = int(n_sequences * 1.1)
                    y.resize(n_sequences, axis=0)
                    X.resize(n_sequences, axis=0)

                # store current X, y in file
                y[i] = y_
                X[i] = X_

            # resize file to exactly match the number of sequences
            y.resize(i, axis=0)
            X.resize(i, axis=0)

    def iter_sequences(self, cache=None):

        # pre-generate all labeled sequences (from the whole training set)

        # in memory
        if cache is None:
            Xy_generator = self.generator_(self.file_generator,
                                           robust=self.robust)
            X, y = zip(*Xy_generator)
            X = np.vstack(X)
            y = np.hstack(y)

        # in HDF5 file
        elif not os.path.isfile(cache):
            Xy_generator = self.generator_(self.file_generator,
                                           robust=self.robust)
            self._precompute(Xy_generator, cache)

        if cache:
            fp = h5py.File(cache, mode='r')
            X = fp['X']
            y = fp['y']

        # keep track of number of labels and rename labels to integers
        unique, y = np.unique(y, return_inverse=True)
        self.n_labels = len(unique)

        generator = random_label_index(
            y, per_label=self.per_label, return_label=False)

        # see __init__ for details on why this is done
        yield

        while True:
            i = next(generator)

            # HACK
            X_, y_ = X[i], y[i]

            if np.any(np.isnan(X_)):
                if self.robust:
                    continue
                else:
                    msg = 'Sequence #{i:d} contains NaNs.'
                    raise ValueError(msg.format(i=i))

            yield X_, y_

        if cache:
            fp.close()

    def __iter__(self):
        return self

    def next(self):
        return self.__next__()

    def __next__(self):
        return next(self.sequence_generator_)

    @property
    def shape(self):
        return self.generator_.shape

    def signature(self):
        shape = self.shape
        return (
            {'type': 'ndarray', 'shape': shape},
            {'type': 'label'}
        )


class DerivativeBatchGenerator(BaseBatchGenerator):
    """

    Generates ([X], derivatives) batch tuples where
      * X are sequences
      * derivatives are ...

    Parameters
    ----------
    feature_extractor: YaafeFeatureExtractor
        Yaafe feature extraction (e.g. YaafeMFCC instance)
    file_generator: iterable
        File generator (the training set, typically)
    compute_derivatives: callable
        ...
    distance: {'sqeuclidean', 'cosine', 'angular'}
        Distance for which the embedding is optimized. Defaults to 'angular'.
    duration: float, optional
    step: float, optional
        Duration and step of sliding window (in seconds). Default to 5s and 2.5s.
    min_duration: float, optional
        Sequence minimum duration. When provided, generates sequences with
        random duration in range [min_duration, duration]. Defaults to
        fixed-duration sequences.
    per_label: int, optional
        Number of samples per label. Defaults to 3.
    per_fold: int, optional
        Number of labels per fold. Defaults to 20.
    per_batch: int, optional
        Number of folds per batch. Defaults to 12.
    n_threads: int, optional
        Defaults to 1.
    cache: str, optional
        Defaults to 'in-memory'
    robust: bool, optional
        When True, skip files for which feature extraction fails.
    """

    def __init__(self, feature_extractor, file_generator, compute_derivatives,
                 distance='angular', duration=5.0, min_duration=None, step=None,
                 heterogeneous=False, per_label=3, per_fold=20, per_batch=12,
                 n_threads=1, cache=None, robust=False):

        self.cache = cache
        self.robust = robust

        self.sequence_generator_ = SequenceGenerator(
            feature_extractor, file_generator,
             duration=duration, step=step, min_duration=min_duration,
             heterogeneous=heterogeneous, per_label=per_label,
             cache=self.cache, robust=self.robust)

        self.n_labels = self.sequence_generator_.n_labels
        self.per_label = per_label
        self.per_fold = per_fold
        self.per_batch = per_batch
        self.n_threads = n_threads

        batch_size = self.per_label * self.per_fold * self.per_batch
        super(DerivativeBatchGenerator, self).__init__(
            self.sequence_generator_, batch_size=batch_size)

        self.compute_derivatives = compute_derivatives

    @property
    def shape(self):
        return self.sequence_generator_.shape

    def get_steps_per_epoch(self, protocol, subset='train'):
        """
        Parameters
        ----------
        protocol : pyannote.database.protocol.protocol.Protocol
        subset : {'train', 'development', 'test'}, optional

        Returns
        -------
        steps_per_epoch : int
            Number of batches per epoch.
        """
        n_folds = self.n_labels / self.per_fold + 1
        return n_folds

    # this callback will make sure the internal embedding is always up to date
    def callbacks(self, extract_embedding=None):
        callback = UpdateGeneratorEmbedding(
            self, extract_embedding=extract_embedding, name='embedding')
        return [callback]

    def postprocess(self, batch):

        sequences, labels = batch

        embeddings = self.embedding.transform(
            sequences, batch_size=self.per_fold * self.per_label)

        [costs, derivatives] = self.compute_derivatives(embeddings, labels)

        return sequences, derivatives
