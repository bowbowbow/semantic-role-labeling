def investigate_pickle():
    import pickle
    file = open('./data/experiments/conll05/train-set.pkl', 'rb')
    data = pickle.load(file)
    print(data[20000])


if __name__ == '__main__':
    investigate_pickle()
